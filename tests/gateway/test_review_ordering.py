"""Review ordering: determinism, signal weights, and the reason for every rank.

Every test here builds the dataclasses by hand — ``rank_files`` is a pure
function of packet fields, so there is no repo, no index and no session store
to set up, and a regression shows up as an ordering diff rather than as a
flaky integration failure.
"""

from __future__ import annotations

import random

from lemoncrow.pro.capabilities.review.models import (
    FILE_CATEGORY_ORDER,
    ChangedFile,
    ChangedSymbol,
    FileCategory,
    FileStatus,
    ImpactKind,
    ImpactSite,
    ReviewOrderEntry,
)
from lemoncrow.pro.capabilities.review.ordering import rank_files


def _file(
    path: str,
    *,
    category: FileCategory = "production",
    status: FileStatus = "modified",
    additions: int = 0,
    deletions: int = 0,
) -> ChangedFile:
    return ChangedFile(
        path=path,
        old_path=None,
        status=status,
        additions=additions,
        deletions=deletions,
        category=category,
    )


def _symbol(
    path: str,
    name: str = "handler",
    *,
    caller_count: int = -1,
    centrality_percentile: float | None = None,
) -> ChangedSymbol:
    return ChangedSymbol(
        symbol_name=name,
        qualified_name=f"mod.{name}",
        kind="function",
        file_path=path,
        start_line=10,
        end_line=20,
        change="modified",
        caller_count=caller_count,
        centrality_percentile=centrality_percentile,
    )


def _site(
    path: str,
    *,
    line: int = 12,
    kind: ImpactKind = "contract_literal",
    in_patch: bool = False,
) -> ImpactSite:
    return ImpactSite(
        kind=kind,
        path=f"{path}:L{line}",
        old="pending",
        new="queued",
        snippet="status == 'pending'",
        in_patch=in_patch,
    )


def _paths(entries: tuple[ReviewOrderEntry, ...]) -> list[str]:
    return [entry.path for entry in entries]


def _by_path(entries: tuple[ReviewOrderEntry, ...]) -> dict[str, ReviewOrderEntry]:
    return {entry.path: entry for entry in entries}


def test_ranking_is_deterministic() -> None:
    """Identical input must produce a byte-identical order, whatever order it arrives in."""

    files = [
        _file("src/a.py", additions=10, deletions=2),
        _file("src/b.py", additions=10, deletions=2),
        _file("src/c.py", additions=300),
        _file("tests/test_a.py", category="test", additions=40),
        _file("docs/guide.md", category="docs", additions=5),
        _file("uv.lock", category="generated", additions=900),
        _file("vendor/x.js", category="vendor", additions=900),
        _file("src/gone.py", status="deleted", deletions=60),
    ]
    symbols = [
        _symbol("src/a.py", "load", caller_count=6),
        _symbol("src/b.py", "save", centrality_percentile=0.95),
        _symbol("src/c.py", "noop"),
    ]
    impact = [
        _site("src/a.py", line=11),
        _site("src/a.py", line=44, kind="signature_change"),
        _site("src/b.py", line=9, in_patch=True),
    ]

    baseline = rank_files(files, symbols, impact)
    repeat = rank_files(files, symbols, impact)
    assert baseline == repeat

    rng = random.Random(1337)
    for _ in range(5):
        shuffled_files = list(files)
        shuffled_symbols = list(symbols)
        shuffled_impact = list(impact)
        rng.shuffle(shuffled_files)
        rng.shuffle(shuffled_symbols)
        rng.shuffle(shuffled_impact)
        assert rank_files(shuffled_files, shuffled_symbols, shuffled_impact) == baseline

    # Ranks are 1-based, contiguous and exhaustive.
    assert [entry.rank for entry in baseline] == list(range(1, len(files) + 1))
    assert sorted(_paths(baseline)) == sorted(item.path for item in files)


def test_high_centrality_outranks_larger_diff() -> None:
    files = [
        _file("src/small.py", additions=5),
        _file("src/huge.py", additions=400),
    ]
    symbols = [_symbol("src/small.py", "core", centrality_percentile=0.95)]

    order = rank_files(files, symbols, ())

    assert _paths(order) == ["src/small.py", "src/huge.py"]
    assert "high-centrality symbol" in order[0].reasons


def test_generated_and_vendor_sink_to_bottom() -> None:
    files = [
        _file("uv.lock", category="generated", additions=4000),
        _file("src/app.py", additions=3),
        _file("vendor/x.js", category="vendor", additions=4000),
        _file("docs/readme.md", category="docs", additions=3),
        _file("tests/test_app.py", category="test", additions=3),
    ]

    order = rank_files(files, (), ())

    assert set(_paths(order)[-2:]) == {"uv.lock", "vendor/x.js"}
    for entry in order[-2:]:
        assert "generated/vendor — skim" in entry.reasons
        assert entry.score < 0


def test_test_category_is_not_itself_an_attention_finding() -> None:
    files = [_file("tests/test_large.py", category="test", additions=300)]

    entry = rank_files(files, (), ())[0]

    assert "test coverage for this change" not in entry.reasons
    assert entry.reasons == ("+300 -0",)


def test_test_symbol_centrality_does_not_compete_with_production_blast_radius() -> None:
    files = [
        _file("tests/test_shared.py", category="test", additions=300),
        _file("src/new_engine.py", status="added", additions=300),
    ]
    symbols = [_symbol("tests/test_shared.py", "helper", caller_count=50, centrality_percentile=0.99)]

    order = rank_files(files, symbols, ())
    entries = _by_path(order)

    assert order[0].path == "src/new_engine.py"
    assert "substantial new production surface" in entries["src/new_engine.py"].reasons
    assert "high-centrality symbol" not in entries["tests/test_shared.py"].reasons
    assert "50 known callers" not in entries["tests/test_shared.py"].reasons


def test_small_new_production_helper_does_not_get_structural_attention_bonus() -> None:
    entry = rank_files([_file("src/helper.py", status="added", additions=40)], (), ())[0]

    assert "substantial new production surface" not in entry.reasons


def test_untouched_impact_dominates() -> None:
    files = [
        _file("src/reaches.py", additions=20),
        _file("src/quiet.py", additions=20),
    ]
    impact = [
        _site("src/reaches.py", line=10),
        _site("src/reaches.py", line=20),
        _site("src/reaches.py", line=30),
    ]

    order = rank_files(files, (), impact)

    assert _paths(order) == ["src/reaches.py", "src/quiet.py"]
    assert "3 untouched impacted site(s)" in order[0].reasons
    assert order[0].score - order[1].score == 9.0


def test_in_patch_sites_do_not_count_as_untouched() -> None:
    files = [_file("src/a.py", additions=10)]
    order = rank_files(files, (), [_site("src/a.py", in_patch=True)])

    assert not any(reason.endswith("untouched impacted site(s)") for reason in order[0].reasons)


def test_untouched_site_bonus_is_capped() -> None:
    files = [_file("src/a.py")]
    sites = [_site("src/a.py", line=index) for index in range(10)]

    order = rank_files(files, (), sites)

    # production 2.0 + capped 9.0; the cap holds even at ten sites.
    assert order[0].score == 11.0
    assert "10 untouched impacted site(s)" in order[0].reasons


def test_caller_bonus_is_capped_and_named() -> None:
    files = [_file("src/a.py"), _file("src/b.py")]
    symbols = [
        _symbol("src/a.py", "many", caller_count=50),
        _symbol("src/b.py", "few", caller_count=2),
    ]

    entries = _by_path(rank_files(files, symbols, ()))

    assert entries["src/a.py"].score == 5.0
    assert "50 known callers" in entries["src/a.py"].reasons
    assert entries["src/b.py"].score == 3.0
    assert "2 known callers" in entries["src/b.py"].reasons


def test_unknown_symbol_signals_never_promote() -> None:
    """The -1 / None sentinels PR-2 emits without an index must contribute nothing."""

    files = [_file("src/a.py"), _file("src/b.py")]
    symbols = [_symbol("src/a.py", "blind", caller_count=-1, centrality_percentile=None)]

    entries = _by_path(rank_files(files, symbols, ()))

    assert entries["src/a.py"].score == entries["src/b.py"].score == 2.0
    assert entries["src/a.py"].reasons == ()


def test_contract_change_kinds_promote_with_one_reason() -> None:
    files = [_file("src/sig.py"), _file("src/removed.py"), _file("src/lit.py")]
    impact = [
        _site("src/sig.py", kind="signature_change"),
        _site("src/removed.py", kind="removed_symbol"),
        _site("src/lit.py", kind="contract_literal"),
    ]

    entries = _by_path(rank_files(files, (), impact))

    for path in ("src/sig.py", "src/removed.py"):
        assert "public contract changed" in entries[path].reasons
    assert "public contract changed" not in entries["src/lit.py"].reasons


def test_deleted_file_is_flagged() -> None:
    files = [_file("src/gone.py", status="deleted", deletions=30)]

    order = rank_files(files, (), ())

    assert "file deleted" in order[0].reasons
    assert order[0].score == round(2.0 + 1.0 + 30 / 200, 4)


def test_reasons_explain_every_promoted_file() -> None:
    files = [
        _file("src/a.py", additions=10, deletions=2),
        _file("src/b.py", additions=400),
        _file("src/quiet.py"),
        _file("tests/test_a.py", category="test", additions=90),
        _file("docs/x.md", category="docs", additions=12),
        _file("pyproject.toml", category="config", additions=4),
        _file("uv.lock", category="generated", additions=2000),
        _file("src/gone.py", status="deleted", deletions=8),
    ]
    symbols = [_symbol("src/a.py", "load", caller_count=6, centrality_percentile=0.99)]
    impact = [_site("src/b.py", kind="signature_change")]

    order = rank_files(files, symbols, impact)

    promoted = [entry for entry in order if entry.score > 2.0]
    assert promoted, "fixture should promote something"
    for entry in promoted:
        assert entry.reasons, f"{entry.path} ranked {entry.rank} with no stated reason"


def test_reasons_are_sorted_and_deduplicated() -> None:
    files = [_file("src/a.py", additions=10)]
    symbols = [
        _symbol("src/a.py", "one", centrality_percentile=0.95),
        _symbol("src/a.py", "two", centrality_percentile=0.97),
    ]
    impact = [
        _site("src/a.py", line=1, kind="signature_change"),
        _site("src/a.py", line=2, kind="removed_symbol"),
    ]

    reasons = rank_files(files, symbols, impact)[0].reasons

    assert list(reasons) == sorted(reasons)
    assert len(reasons) == len(set(reasons))
    assert reasons.count("high-centrality symbol") == 1
    assert reasons.count("public contract changed") == 1


def test_ties_broken_by_category_then_path() -> None:
    # Both score exactly 2.0: production prior alone vs. test prior + 300 lines.
    files = [
        _file("tests/aaa_test.py", category="test", additions=300),
        _file("src/zzz.py"),
    ]

    order = rank_files(files, (), ())

    assert [entry.score for entry in order] == [2.0, 2.0]
    assert _paths(order) == ["src/zzz.py", "tests/aaa_test.py"]
    assert FILE_CATEGORY_ORDER.index("production") < FILE_CATEGORY_ORDER.index("test")

    # Same score and same category -> lexicographic path.
    same = [_file("src/b/x.py"), _file("src/a/x.py"), _file("src/a/a.py")]
    assert _paths(rank_files(same, (), ())) == ["src/a/a.py", "src/a/x.py", "src/b/x.py"]


def test_uninspected_bonus_applies_only_with_impact() -> None:
    files = [
        _file("src/read.py", additions=10),
        _file("src/unread.py", additions=10),
        _file("src/no_impact.py", additions=10),
    ]
    impact = [_site("src/read.py"), _site("src/unread.py")]

    # R2: nothing recorded -> the signal is off entirely, impact sites or not.
    blind = _by_path(rank_files(files, (), impact, inspected=frozenset()))
    for entry in blind.values():
        assert "not inspected by the generating agent" not in entry.reasons

    # Recorded reads are host-absolute; the match is on the repo-relative tail.
    seen = _by_path(rank_files(files, (), impact, inspected=frozenset({"/home/dev/repo/src/read.py"})))
    assert "not inspected by the generating agent" not in seen["src/read.py"].reasons
    assert "not inspected by the generating agent" in seen["src/unread.py"].reasons
    # No impact sites -> no claim, even though it was never read.
    assert "not inspected by the generating agent" not in seen["src/no_impact.py"].reasons


def test_group_mirrors_file_category() -> None:
    files = [
        _file("src/a.py"),
        _file("tests/t.py", category="test"),
        _file("uv.lock", category="generated"),
        _file("vendor/v.js", category="vendor"),
        _file("docs/d.md", category="docs"),
        _file("pyproject.toml", category="config"),
    ]

    entries = _by_path(rank_files(files, (), ()))

    for item in files:
        assert entries[item.path].group == item.category


def test_scores_are_rounded_to_four_places() -> None:
    files = [_file("src/a.py", additions=7)]  # 7/200 = 0.035

    order = rank_files(files, (), ())

    assert order[0].score == round(order[0].score, 4)
    assert order[0].score == 2.035
    assert "+7 -0" in order[0].reasons


def test_zero_churn_emits_no_size_reason() -> None:
    order = rank_files([_file("src/a.py")], (), ())

    assert order[0].reasons == ()
    assert order[0].score == 2.0


def test_empty_input_returns_empty_order() -> None:
    assert rank_files((), (), ()) == ()


def test_site_path_without_line_suffix_still_matches() -> None:
    """A site whose path carries no ``:L<n>`` tail must still attach to its file."""

    files = [_file("src/a.py"), _file("src/b.py")]
    bare = ImpactSite(
        kind="untouched_caller",
        path="src/a.py",
        old="load",
        new=None,
        snippet="load()",
    )

    entries = _by_path(rank_files(files, (), [bare]))

    assert "1 untouched impacted site(s)" in entries["src/a.py"].reasons
    assert entries["src/b.py"].reasons == ()


def test_unknown_category_sorts_last_without_raising() -> None:
    """A category outside FILE_CATEGORY_ORDER must degrade, not raise."""

    odd: FileCategory = "experimental"  # type: ignore[assignment]
    files = [_file("z/odd.py", category=odd), _file("a/prod.py", category="production")]

    order = rank_files(files, (), ())

    assert _paths(order) == ["a/prod.py", "z/odd.py"]
    assert order[1].score == 0.0
