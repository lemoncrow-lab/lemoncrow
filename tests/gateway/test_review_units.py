"""Review units: identity that survives a move, fingerprints that do not survive a rewrite.

These two properties are the product, so they are asserted as properties rather
than as snapshots. The load-bearing one is
:func:`test_inserting_ten_lines_above_a_function_changes_neither_key_nor_fingerprint`:
agents rewrite files constantly, and a review frontier whose keys noticed a line
number would evaporate every mark in a file for an edit made to none of them.

One test here pins a *limitation* rather than a feature
(:func:`test_a_decorator_only_edit_changes_the_file_fingerprint_but_not_the_symbol`).
The symbol window starts at the ``def`` keyword, so a decorator edit is charged
to the file and not to the definition below it. That is deliberate -- the
prelude is what separates two neighbouring definitions -- and it is asserted so
the day someone changes it, they change it on purpose.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from lemoncrow.pro.capabilities.review.impact import SymbolWindow, symbol_windows
from lemoncrow.pro.capabilities.review.models import (
    ChangedFile,
    ChangedSymbol,
    DiffHunk,
    ReviewOrderEntry,
    ReviewPacket,
    empty_stats,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewUnit
from lemoncrow.pro.capabilities.review.units import (
    derive_units,
    file_fingerprint,
    module_data_name,
    normalize_body,
    symbol_fingerprint,
    tree_fingerprint,
    unit_key,
)

_MODULE = 'import functools\n\n\n@functools.lru_cache\ndef alpha(value):\n    """Doc."""\n    inner = value + 1\n\n    def helper(x):\n        return x * 2\n\n    return helper(inner)\n\n\nclass Beta:\n    def gamma(self):\n        return 3\n'
_TYPESCRIPT = 'export function alpha(a: number): number {\n  return a + 1;\n}\n\nexport class Beta {\n  gamma(): string {\n    return "x";\n  }\n}\n'


def _windows(text: str, path: str = "pkg/mod.py") -> dict[str, SymbolWindow]:
    return {window.name: window for window in symbol_windows(text, path)}


def _packet(
    *,
    files: tuple[ChangedFile, ...] = (),
    symbols: tuple[ChangedSymbol, ...] = (),
    order: tuple[ReviewOrderEntry, ...] = (),
) -> ReviewPacket:
    """A minimal packet: only the four slices ``derive_units`` actually reads."""
    return ReviewPacket(
        schema_version=2,
        generated_at="2026-09-08T00:00:00+00:00",
        repo_root="/repo",
        range_mode="working_tree",
        base_rev="HEAD",
        head_rev="WORKDIR",
        base_sha="0" * 40,
        head_sha="",
        files=files,
        symbols=symbols,
        order=order,
        stats=empty_stats(),
    )


def _hunk(new_start: int, new_lines: int, *, ranges: tuple[tuple[int, int], ...], patch: str = "") -> DiffHunk:
    return DiffHunk(
        old_start=new_start,
        old_lines=new_lines,
        new_start=new_start,
        new_lines=new_lines,
        header=f"@@ -{new_start},{new_lines} +{new_start},{new_lines} @@",
        added=new_lines,
        removed=0,
        new_ranges=ranges,
        patch=patch,
    )


@pytest.mark.parametrize("count", [0, 1, 2249])
def test_file_progress_counts_completed_files_including_reused_projections(count: int, monkeypatch) -> None:
    from lemoncrow.pro.capabilities.review import units as units_module

    # Each file takes a full reporting interval; no sleeps or real clock needed.
    tick = iter(i * 0.25 for i in range(2 * count + 4))
    monkeypatch.setattr(units_module.time, "monotonic", lambda: next(tick))
    packet = _packet(
        files=tuple(ChangedFile(path=f"file-{i}.py", old_path=None, status="modified") for i in range(count))
    )
    blobs = {item.path: "text\n" for item in packet.files}
    original = derive_units(packet, blobs)
    tick = iter(i * 0.25 for i in range(2 * count + 4))
    seen = []
    result = derive_units(
        packet,
        blobs,
        previous_packet=packet,
        previous_units=original,
        previous_blobs=blobs,
        progress=lambda done, total: seen.append((done, total)),
    )
    assert result == original
    assert seen[0] == (0, count)
    assert seen[-1] == (count, count)
    assert {done for done, _ in seen} == set(range(count + 1))
    assert all(total == count for _, total in seen)


def test_review_unit_fingerprint_contract() -> None:
    """case: test_a_decorator_only_edit_changes_the_file_fingerprint_but_not_the_symbol"""
    "The documented limitation of §4.2, asserted so it cannot regress silently."
    after = _MODULE.replace("@functools.lru_cache\n", "@functools.lru_cache(maxsize=8)\n")
    assert after != _MODULE
    before_window = _windows(_MODULE)["alpha"]
    after_window = _windows(after)["alpha"]
    assert symbol_fingerprint(before_window.kind, "alpha", before_window.body) == symbol_fingerprint(
        after_window.kind, "alpha", after_window.body
    )
    assert file_fingerprint("pkg/mod.py", _MODULE) != file_fingerprint("pkg/mod.py", after)
    "case: test_tree_fingerprint_ignores_parser_and_symbol_projection_drift"
    "Revision identity is reviewed file bytes, not today's analysis vocabulary."
    file_unit = _unit("fil:a", "file-bytes")
    old_symbol = ReviewUnit(
        revision_id="",
        unit_key="sym:old",
        kind="symbol",
        path="pkg/mod.py",
        symbol="evidence",
        content_fingerprint="symbol-body",
    )
    renamed_projection = ReviewUnit(
        revision_id="",
        unit_key="sym:new",
        kind="symbol",
        path="pkg/mod.py",
        symbol="detail.evidence",
        content_fingerprint="symbol-body",
    )
    assert tree_fingerprint([file_unit, old_symbol]) == tree_fingerprint([file_unit, renamed_projection])
    "case: test_changed_projection_matches_fresh_units"
    for change in ["bytes", "whitespace", "hunks", "symbols", "reasons", "missing", "unknown"]:
        path = "pkg/mod.py"
        source = "def alpha():\n    return 1\n"
        packet = _packet(
            files=(ChangedFile(path=path, old_path=None, status="added", hunks=(_hunk(1, 2, ranges=((1, 2),)),)),),
            symbols=(
                ChangedSymbol(
                    symbol_name="alpha",
                    qualified_name="alpha",
                    kind="function",
                    file_path=path,
                    start_line=1,
                    end_line=2,
                    change="added",
                ),
            ),
        )
        blobs = {path: source}
        previous = derive_units(packet, blobs)
        current = packet
        current_blobs = blobs
        if change == "bytes":
            current_blobs = {path: source.replace("return 1", "return 2")}
        elif change == "whitespace":
            current_blobs = {path: source.replace("return 1", "return 1  ")}
        elif change == "hunks":
            current = replace(packet, files=(replace(packet.files[0], hunks=()),))
        elif change == "symbols":
            current = replace(packet, symbols=())
        elif change == "reasons":
            current = replace(
                packet, order=(ReviewOrderEntry(path=path, rank=1, score=1.0, group="code", reasons=("changed",)),)
            )
        elif change == "unknown":
            previous = tuple(
                replace(unit, fingerprint_method="unknown") if unit.kind == "file" else unit for unit in previous
            )
        actual = derive_units(
            current,
            current_blobs,
            previous_packet=packet,
            previous_units=previous,
            previous_blobs=None if change == "missing" else blobs,
        )
        assert actual == derive_units(current, current_blobs)
    "case: test_nearby_module_values_get_one_stable_section_unit"
    source = '__all__ = [\n    "one",\n    "two",\n    "three",\n    "four",\n    "five",\n]\nDEBOUNCE_S: Final[float] = 0.5\n# Wait after a tool call.\nIDLE_CHECK_S: Final[float] = 30.0\n_LOG = logging.getLogger(__name__)\nSyncResult = bool | None\n\ndef run():\n    return DEBOUNCE_S\n'
    lines = source.splitlines()
    hunk = _hunk(1, len(lines), ranges=((1, len(lines)),), patch="".join(f"+{line}\n" for line in lines))
    packet = _packet(
        files=(ChangedFile(path="pkg/mod.py", old_path=None, status="added", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="DEBOUNCE_S",
                qualified_name="DEBOUNCE_S",
                kind="variable",
                file_path="pkg/mod.py",
                start_line=8,
                end_line=8,
                change="added",
            ),
        ),
    )
    units = derive_units(packet, {"pkg/mod.py": source})
    sections = [unit for unit in units if unit.kind == "document_section"]
    assert len(sections) == 1
    assert (sections[0].start_line, sections[0].end_line) == (8, 12)
    assert sections[0].fingerprint_method == "symbol_body_sha256"
    assert {unit.symbol for unit in units if unit.kind == "symbol"} >= {"__all__", "DEBOUNCE_S", "run"}
    revised = source.replace("30.0", "31.0")
    changed = next(unit for unit in derive_units(packet, {"pkg/mod.py": revised}) if unit.kind == "document_section")
    assert changed.unit_key == sections[0].unit_key
    assert changed.content_fingerprint != sections[0].content_fingerprint
    "case: test_module_value_group_stops_at_callable_and_code_boundaries"
    source = "A = 1\nB = 2\ndef work():\n    return A\nC = 3\nD = 4\n"
    windows = symbol_windows(source, "pkg/mod.py")
    from lemoncrow.pro.capabilities.review.units import _module_data_groups

    groups = _module_data_groups(windows, source.splitlines())
    assert [[window.name for window in group] for group in groups] == [["A", "B"], ["C", "D"]]


def test_review_unit_derivation_contract() -> None:
    """case: test_derive_units_emits_one_file_unit_one_hunk_unit_and_the_touched_symbols"""
    hunk = _hunk(15, 3, ranges=((16, 16),), patch=" class Beta:\n-        return 2\n+        return 3\n")
    packet = _packet(
        files=(ChangedFile(path="pkg/mod.py", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="gamma",
                qualified_name="Beta.gamma",
                kind="method",
                file_path="pkg/mod.py",
                start_line=16,
                end_line=17,
                change="modified",
                caller_count=4,
                centrality_rank=2,
            ),
        ),
        order=(ReviewOrderEntry(path="pkg/mod.py", rank=1, score=9.0, reasons=("4 known callers",)),),
    )
    units = derive_units(packet, {"pkg/mod.py": _MODULE})
    kinds = [unit.kind for unit in units]
    assert kinds.count("file") == 1
    assert kinds.count("hunk") == 1
    assert "symbol" in kinds
    symbols = {unit.symbol: unit for unit in units if unit.kind == "symbol"}
    assert set(symbols) == {"Beta", "Beta.gamma"}
    assert symbols["Beta.gamma"].fingerprint_method == "symbol_body_sha256"
    assert symbols["Beta.gamma"].reasons == ("4 known callers", "symbol modified", "centrality rank 2")
    "case: test_only_definitions_a_hunk_actually_touched_become_symbol_units"
    "``alpha`` is in the file and in ``packet.symbols``; no hunk reaches it."
    hunk = _hunk(15, 3, ranges=((16, 16),))
    packet = _packet(
        files=(ChangedFile(path="pkg/mod.py", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="gamma",
                qualified_name="Beta.gamma",
                kind="method",
                file_path="pkg/mod.py",
                start_line=16,
                end_line=17,
                change="modified",
            ),
        ),
    )
    names = {unit.symbol for unit in derive_units(packet, {"pkg/mod.py": _MODULE}) if unit.kind == "symbol"}
    assert "alpha" not in names
    "case: test_a_hunk_with_no_ranges_widens_rather_than_dropping_every_symbol"
    '``new_ranges == ()`` means "unreadable patch body", never "nothing changed".'
    hunk = _hunk(1, 20, ranges=())
    packet = _packet(
        files=(ChangedFile(path="pkg/mod.py", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="alpha",
                qualified_name="alpha",
                kind="function",
                file_path="pkg/mod.py",
                start_line=5,
                end_line=12,
                change="modified",
            ),
        ),
    )
    names = {unit.symbol for unit in derive_units(packet, {"pkg/mod.py": _MODULE}) if unit.kind == "symbol"}
    assert "alpha" in names
    "case: test_an_absent_blob_is_unknown_where_an_empty_one_is_content"
    'Absence is the signal, and it has to survive all the way to the mark.\n\n    ``load_blobs`` used to store ``""`` for a file it could not read (over the\n    blob cap, or an unmerged path with no stage-0 entry). The key was therefore\n    present, this branch never fired, and the file was fingerprinted over empty\n    text under ``blob_sha256`` -- a per-path constant that does not move however\n    the file is rewritten, so a ``reviewed`` mark survived a full regeneration of\n    content nobody had seen. Both downgrade doors key on\n    ``fingerprint_method == "unknown"``, so the absent key must reach them, and\n    an *actually* empty file must not be dragged along with it: an empty file is\n    a file we looked at.\n    '
    from lemoncrow.pro.capabilities.review.revisions import _next_state
    from lemoncrow.pro.capabilities.review.session_models import ReviewMark
    from lemoncrow.pro.capabilities.review.sources.local import effective_mark_state

    packet = _packet(files=(ChangedFile(path="uv.lock", old_path=None, status="modified"),))
    unread = derive_units(packet, {})[0]
    empty = derive_units(packet, {"uv.lock": ""})[0]
    assert unread.fingerprint_method == "unknown"
    assert empty.fingerprint_method == "blob_sha256"
    assert unread.content_fingerprint != empty.content_fingerprint
    assert unread.unit_key == empty.unit_key, "identity does not depend on whether we could read it"
    assert effective_mark_state(unread, "reviewed") == "unknown"
    assert effective_mark_state(empty, "reviewed") == "reviewed"
    mark = ReviewMark(
        review_id="r1",
        unit_key=unread.unit_key,
        state="reviewed",
        reviewed_revision_id="rev1",
        content_fingerprint=unread.content_fingerprint,
    )
    assert _next_state(mark, unread, None, {}) == ("unknown", "fingerprint_unknown")
    assert _next_state(mark, empty, None, {}) == ("changed_since_review", "content_changed")
    "case: test_a_hunk_without_a_captured_body_falls_back_to_geometry_with_unknown_method"
    captured = _hunk(1, 2, ranges=((1, 2),), patch="+a\n+b\n")
    bare = _hunk(1, 2, ranges=((1, 2),))
    with_body = derive_units(
        _packet(files=(ChangedFile(path="pkg/x.py", old_path=None, status="modified", hunks=(captured,)),)),
        {"pkg/x.py": "a\nb\n"},
    )
    without = derive_units(
        _packet(files=(ChangedFile(path="pkg/x.py", old_path=None, status="modified", hunks=(bare,)),)),
        {"pkg/x.py": "a\nb\n"},
    )
    assert with_body[1].fingerprint_method == "hunk_patch_sha256"
    assert without[1].fingerprint_method == "unknown"
    assert without[1].content_fingerprint != ""
    assert with_body[1].unit_key == without[1].unit_key
    "case: test_a_deleted_symbol_gets_a_synthetic_unknown_fingerprint"
    hunk = _hunk(1, 2, ranges=((1, 2),))
    packet = _packet(
        files=(ChangedFile(path="pkg/mod.py", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="vanished",
                qualified_name="vanished",
                kind="function",
                file_path="pkg/mod.py",
                start_line=40,
                end_line=40,
                change="deleted",
            ),
        ),
    )
    deleted = [unit for unit in derive_units(packet, {"pkg/mod.py": _MODULE}) if unit.symbol == "vanished"]
    assert len(deleted) == 1
    assert deleted[0].fingerprint_method == "unknown"
    assert deleted[0].content_fingerprint != ""


def test_review_unit_symbol_identity_contract() -> None:
    """case: test_same_named_methods_in_one_file_are_named_by_their_class_not_by_an_ordinal"""
    "Two ``run`` methods must not both be called ``pkg/two.py::run``.\n\n    Distinct ``unit_key``s were never the problem -- nothing false survived in\n    the store. The *rows* were the problem: a reviewer sent to ``run`` arrives\n    at one of two methods and has no way to tell which, and in a real repository\n    the same name showed up in UNCHANGED REVIEWED and NEW SINCE MY LAST REVIEW\n    at the same time. Nesting is the disambiguator the code already carries, and\n    it survives the insertion that an ordinal or a line number would not.\n    "
    text = "class One:\n    def run(self):\n        return 1\n\n\nclass Two:\n    def run(self):\n        return 2\n"
    hunk = _hunk(1, 8, ranges=((1, 8),))
    packet = _packet(
        files=(ChangedFile(path="pkg/two.py", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="run",
                qualified_name=None,
                kind="method",
                file_path="pkg/two.py",
                start_line=2,
                end_line=3,
                change="modified",
            ),
        ),
    )
    runs = [unit for unit in derive_units(packet, {"pkg/two.py": text}) if unit.symbol.endswith("run")]
    assert [unit.symbol for unit in runs] == ["One.run", "Two.run"]
    assert runs[0].unit_key != runs[1].unit_key
    assert len({unit.content_fingerprint for unit in runs}) == 2
    "case: test_stale_index_parent_cannot_collapse_sibling_methods_onto_one_unit_key"
    text = "class CommandRunner:\n    def run(self):\n        return 1\n\nclass HttpServiceRunner:\n    def run(self):\n        return 2\n"
    hunk = _hunk(1, 7, ranges=((1, 7),))
    packet = _packet(
        files=(ChangedFile(path="runtimes.py", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="run",
                qualified_name="ReviewRunner.run",
                kind="method",
                file_path="runtimes.py",
                start_line=2,
                end_line=3,
                change="modified",
            ),
            ChangedSymbol(
                symbol_name="run",
                qualified_name="ReviewRunner.run",
                kind="method",
                file_path="runtimes.py",
                start_line=6,
                end_line=7,
                change="modified",
            ),
        ),
    )
    runs = [unit for unit in derive_units(packet, {"runtimes.py": text}) if unit.symbol.endswith("run")]
    assert [unit.symbol for unit in runs] == ["CommandRunner.run", "HttpServiceRunner.run"]
    assert len({unit.unit_key for unit in runs}) == 2
    "case: test_index_bare_name_cannot_collapse_scoped_shell_assignments_onto_one_unit_key"
    "A lossy index name must not erase scope recovered from containment.\n\n    Shell indexing can report both a module assignment and an assignment inside\n    a function as the same bare qualified name. That used to produce two\n    ``ReviewUnit`` rows with the same ``(revision_id, unit_key)`` and made a bare\n    ``lc review`` die in ``ReviewStore.add_revision``.\n    "
    text = 'LEMONCROW_TELEGRAPHIC="${LEMONCROW_TELEGRAPHIC:-}"\nprompt_telegraphic_selection() {\n    LEMONCROW_TELEGRAPHIC="lite"\n}\n'
    hunk = _hunk(1, 4, ranges=((1, 4),))
    packet = _packet(
        files=(ChangedFile(path="scripts/lib/common.sh", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="LEMONCROW_TELEGRAPHIC",
                qualified_name="LEMONCROW_TELEGRAPHIC",
                kind="variable",
                file_path="scripts/lib/common.sh",
                start_line=1,
                end_line=1,
                change="modified",
            ),
            ChangedSymbol(
                symbol_name="LEMONCROW_TELEGRAPHIC",
                qualified_name="LEMONCROW_TELEGRAPHIC",
                kind="variable",
                file_path="scripts/lib/common.sh",
                start_line=3,
                end_line=3,
                change="modified",
            ),
        ),
    )
    assignments = [
        unit
        for unit in derive_units(packet, {"scripts/lib/common.sh": text})
        if unit.symbol.endswith("LEMONCROW_TELEGRAPHIC")
    ]
    assert [unit.symbol for unit in assignments] == [
        "LEMONCROW_TELEGRAPHIC",
        "prompt_telegraphic_selection.LEMONCROW_TELEGRAPHIC",
    ]
    assert len({unit.unit_key for unit in assignments}) == 2
    "case: test_two_definitions_nesting_cannot_separate_still_fall_back_to_the_ordinal"
    "Containment is not always enough, and the ordinal is still the backstop.\n\n    Two module-level ``run``s qualify to the same name, so ``unit_key`` has\n    nothing but the ordinal left to keep them apart. The keys must still differ;\n    naming them apart on screen is then the label's job (``@L2`` / ``@L6``).\n    "
    text = "def run():\n    return 1\n\n\ndef run():\n    return 2\n"
    hunk = _hunk(1, 6, ranges=((1, 6),))
    packet = _packet(
        files=(ChangedFile(path="pkg/flat.py", old_path=None, status="modified", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="run",
                qualified_name=None,
                kind="function",
                file_path="pkg/flat.py",
                start_line=1,
                end_line=2,
                change="modified",
            ),
        ),
    )
    runs = [unit for unit in derive_units(packet, {"pkg/flat.py": text}) if unit.symbol == "run"]
    assert [unit.ordinal for unit in runs] == [0, 1]
    assert [unit.start_line for unit in runs] == [1, 5]
    assert runs[0].unit_key != runs[1].unit_key
    "case: test_module_data_name_names_data_and_never_a_callable"
    for line, name in [
        ("const LIMIT = 5", "LIMIT"),
        ("export const total = (a + b) * 2", "total"),
        ("const mode = ready ? fast : slow", "mode"),
        ("const asyncValue = asyncDefault", "asyncValue"),
        ("const load = (id) => fetch(id)", None),
        ("const load = id => fetch(id)", None),
        ("const load = async () => {", None),
        ("export const load = async (id: string) => fetch(id)", None),
        ("const load = async id => fetch(id)", None),
        ("export const parse = (text: string): Result<Tree> => {", None),
        ("const identity = <T,>(value: T) => value", None),
        ("const handler: Handler = async (event) => {", None),
        ("const run = async function () {", None),
        ("square = lambda value: value * value", None),
    ]:
        assert module_data_name(line) == name


def test_review_unit_ranking_and_store_contract() -> None:
    """case: test_attention_rank_group_and_reasons_are_inherited_by_every_unit_of_the_file"""
    hunk = _hunk(1, 2, ranges=((1, 2),), patch="+a\n")
    packet = _packet(
        files=(ChangedFile(path="pkg/x.py", old_path=None, status="modified", hunks=(hunk,)),),
        order=(
            ReviewOrderEntry(path="pkg/x.py", rank=3, score=1.0, reasons=("public contract changed",), group="test"),
        ),
    )
    for unit in derive_units(packet, {"pkg/x.py": "a\n"}):
        assert unit.attention_rank == 3
        assert unit.attention_group == "test"
        assert unit.reasons == ("public contract changed",)
    "case: test_an_unranked_file_gets_rank_zero_rather_than_going_missing"
    "``--limit`` truncates the order, never the change."
    packet = _packet(
        files=(
            ChangedFile(path="pkg/ranked.py", old_path=None, status="modified"),
            ChangedFile(path="pkg/unranked.py", old_path=None, status="modified", category="docs"),
        ),
        order=(ReviewOrderEntry(path="pkg/ranked.py", rank=1, score=1.0),),
    )
    units = {unit.path: unit for unit in derive_units(packet, {"pkg/ranked.py": "a", "pkg/unranked.py": "b"})}
    assert units["pkg/ranked.py"].attention_rank == 1
    assert units["pkg/unranked.py"].attention_rank == 0
    assert units["pkg/unranked.py"].attention_group == "docs"
    "case: test_derive_units_leaves_revision_id_blank_for_the_store_to_stamp"
    packet = _packet(files=(ChangedFile(path="pkg/x.py", old_path=None, status="modified"),))
    assert all(unit.revision_id == "" for unit in derive_units(packet, {"pkg/x.py": "a"}))
    "case: test_derive_units_over_an_empty_packet_returns_nothing_and_does_not_raise"
    assert derive_units(_packet(), {}) == ()
    "case: test_no_unit_ever_carries_an_empty_content_fingerprint"
    hunk = _hunk(15, 3, ranges=((16, 16),))
    packet = _packet(
        files=(
            ChangedFile(path="pkg/mod.py", old_path=None, status="modified", hunks=(hunk,)),
            ChangedFile(path="assets/logo.png", old_path=None, status="modified", is_binary=True),
            ChangedFile(path="pkg/gone.py", old_path="pkg/gone.py", status="deleted"),
        ),
        symbols=(
            ChangedSymbol(
                symbol_name="gamma",
                qualified_name="Beta.gamma",
                kind="method",
                file_path="pkg/mod.py",
                start_line=16,
                end_line=17,
                change="modified",
            ),
        ),
    )
    for unit in derive_units(packet, {"pkg/mod.py": _MODULE}):
        assert unit.content_fingerprint != ""
    "case: test_derive_units_mints_no_unit_for_an_imported_binding"
    "The gate is in the window seam, so it holds for units without a second copy."
    hunk = _hunk(1, 9, ranges=((1, 9),), patch='+import { afterEach } from "vitest";\n')
    packet = _packet(
        files=(ChangedFile(path="src/reviewApi.test.ts", old_path=None, status="added", hunks=(hunk,)),),
        symbols=(
            ChangedSymbol(
                symbol_name="navigateTo",
                qualified_name="navigateTo",
                kind="function",
                file_path="src/reviewApi.test.ts",
                start_line=3,
                end_line=5,
                change="added",
            ),
        ),
    )
    units = derive_units(packet, {"src/reviewApi.test.ts": _VITEST_SUITE})
    names = {unit.symbol for unit in units if unit.kind == "symbol"}
    assert "afterEach" not in names
    assert names == {"navigateTo"}


def test_review_unit_window_and_key_contract() -> None:
    """case: test_symbol_window_contract"""
    windows = symbol_windows(_MODULE, "pkg/mod.py")
    assert [window.name for window in windows] == ["alpha", "helper", "Beta", "gamma"]
    lines = _MODULE.splitlines()
    for window in windows:
        assert window.body == "\n".join(lines[window.start_line - 1 : window.end_line]).strip()
    alpha = _windows(_MODULE)["alpha"]
    assert _MODULE.splitlines()[alpha.start_line - 1].startswith("def alpha") and "lru_cache" not in alpha.body
    assert symbol_windows("def broken(:\n    pass\n", "pkg/bad.py") == ()
    assert symbol_windows("", "pkg/empty.py") == ()
    ts = symbol_windows(_TYPESCRIPT, "src/app.ts")
    assert [window.name for window in ts] == ["alpha", "Beta", "gamma"]
    assert ts[0].start_line == 1 and ts[0].end_line >= 3 and ("return a + 1;" in ts[0].body)
    "case: test_content_stable_fingerprint_contract"
    noisy = "\n".join(f"{line}   " for line in _MODULE.splitlines()) + "\n\n\n"
    assert normalize_body(noisy) == normalize_body(_MODULE)
    assert file_fingerprint("pkg/mod.py", noisy) == file_fingerprint("pkg/mod.py", _MODULE)
    assert file_fingerprint("pkg/mod.py", _MODULE.replace("\n", "\r\n")) == file_fingerprint("pkg/mod.py", _MODULE)
    before = _windows(_MODULE)["gamma"]
    after = _windows(noisy)["gamma"]
    assert symbol_fingerprint("method", "Beta.gamma", before.body) == symbol_fingerprint(
        "method", "Beta.gamma", after.body
    )
    shifted = "# padding\n" * 10 + _MODULE
    shifted_gamma = _windows(shifted)["gamma"]
    assert shifted_gamma.start_line == before.start_line + 10
    assert symbol_fingerprint("method", "Beta.gamma", before.body) == symbol_fingerprint(
        "method", "Beta.gamma", shifted_gamma.body
    )
    "case: test_unit_key_identity_contract"
    assert unit_key("symbol", "pkg/mod.py", "alpha", 0) != unit_key("symbol", "pkg/mod.py", "alpha_renamed", 0)
    assert unit_key("file", "pkg/mod.py", "", 0) != unit_key("file", "pkg/moved.py", "", 0)
    window = _windows(_MODULE)["gamma"]
    moved = _windows(_MODULE, "pkg/moved.py")["gamma"]
    assert symbol_fingerprint("method", "Beta.gamma", window.body) == symbol_fingerprint(
        "method", "Beta.gamma", moved.body
    )
    assert unit_key("symbol", "pkg/mod.py", "alpha", 0) != unit_key("symbol", "pkg/mod.py", "alpha", 1)
    assert unit_key("file", "pkg/mod.py", "", 0).startswith("fil:")
    assert unit_key("hunk", "pkg/mod.py", "", 3).startswith("hun:")
    assert unit_key("symbol", "pkg/mod.py", "alpha", 0).startswith("sym:")
    assert unit_key("file", "ab", "c", 0) != unit_key("file", "a", "bc", 0)
    "case: test_tree_fingerprint_contract"
    units = [_unit("fil:a", "one"), _unit("fil:b", "two"), _unit("fil:c", "three")]
    assert tree_fingerprint(units) == tree_fingerprint(list(reversed(units))) == tree_fingerprint(units)
    changed = [_unit("fil:a", "one"), _unit("fil:b", "two-prime"), _unit("fil:c", "three")]
    assert tree_fingerprint(units) != tree_fingerprint(changed)
    assert tree_fingerprint(()) and tree_fingerprint(()) == tree_fingerprint(())


def test_review_unit_fallback_contract() -> None:
    """case: test_derive_units_file_fallback_contract"""
    hunk = _hunk(1, 2, ranges=((1, 2),), patch="+a\n+b\n")
    plain = derive_units(
        _packet(files=(ChangedFile(path="docs/readme.md", old_path=None, status="modified", hunks=(hunk,)),)),
        {"docs/readme.md": "a\nb\n"},
    )
    assert [unit.kind for unit in plain] == ["file", "hunk"]
    cases = [
        (ChangedFile(path="assets/logo.png", old_path=None, status="modified", is_binary=True), {}, "file"),
        (ChangedFile(path="pkg/gone.py", old_path="pkg/gone.py", status="deleted"), {"pkg/gone.py": ""}, "file"),
        (ChangedFile(path="pkg/huge.py", old_path=None, status="modified"), {}, "file"),
    ]
    for changed_file, blobs, kind in cases:
        units = derive_units(_packet(files=(changed_file,)), blobs)
        assert (
            units
            and units[0].kind == kind
            and (units[0].fingerprint_method == "unknown")
            and units[0].content_fingerprint
        )
    "case: test_non_definition_shapes_never_become_windows"
    names = set(_windows(_VITEST_SUITE, "src/reviewApi.test.ts"))
    assert "afterEach" not in names and "describe" not in names and ("navigateTo" in names)
    for name in _windows(_TSX_TERNARY, "src/App.tsx"):
        assert '"' not in name and " " not in name
    for source, path in [
        (_PACKAGE_JSON, "package.json"),
        ('[project]\nname = "app"\nversion = "1.0"\n', "pyproject.toml"),
        ("jobs:\n  build:\n    runs-on: ubuntu\n", ".github/workflows/ci.yml"),
    ]:
        assert _windows(source, path) == {}


def _unit(key: str, fingerprint: str) -> ReviewUnit:
    return ReviewUnit(revision_id="", unit_key=key, kind="file", path="pkg/mod.py", content_fingerprint=fingerprint)


def test_unchanged_frozen_file_reuses_units_and_refreshes_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import units as units_module

    path = "pkg/mod.py"
    source = "def alpha():\n    return 1\n"
    packet = _packet(
        files=(ChangedFile(path=path, old_path=None, status="added", hunks=(_hunk(1, 2, ranges=((1, 2),)),)),),
        symbols=(
            ChangedSymbol(
                symbol_name="alpha",
                qualified_name="alpha",
                kind="function",
                file_path=path,
                start_line=1,
                end_line=2,
                change="added",
            ),
        ),
    )
    blobs = {path: source}
    previous = derive_units(packet, blobs)
    reranked = replace(packet, order=(ReviewOrderEntry(path=path, rank=3, score=3.0, group="code", reasons=()),))
    expected = derive_units(reranked, blobs)
    monkeypatch.setattr(units_module, "symbol_windows", lambda *_args: pytest.fail("unchanged file was parsed"))
    actual = derive_units(reranked, blobs, previous_packet=packet, previous_units=previous, previous_blobs=blobs)
    assert actual == expected
    assert any(unit.kind == "symbol" for unit in actual)


_VITEST_SUITE = 'import { afterEach, describe } from "vitest";\n\nexport function navigateTo(url: string): void {\n  window.history.replaceState({}, "", url);\n}\n\nafterEach(() => {\n  navigateTo("/");\n});\n'
_TSX_TERNARY = 'export function Shell({ bare }: { bare: boolean }) {\n  return (\n    <div className={bare ? "min-h-full font-mono" : "min-h-full bg-tint"}>\n      {bare}\n    </div>\n  );\n}\n'
_PACKAGE_JSON = '{\n  "name": "app",\n  "dependencies": {\n    "react": "19.0.0"\n  }\n}\n'


def test_units_are_stamped_with_their_structural_answer_and_only_a_marked_file_is_accepted_by_rule() -> None:
    from lemoncrow.pro.capabilities.review.units import stamp_answers

    symlink = ChangedFile(
        path="link",
        old_path=None,
        status="added",
        new_mode=0o120000,
        hunks=(_hunk(1, 1, ranges=((1, 1),), patch="+target.txt\n"),),
    )
    submodule = ChangedFile(path="vendor/lib", old_path=None, status="modified", submodule_pointer=("a" * 40, "b" * 40))
    executable = ChangedFile(path="run.sh", old_path=None, status="modified", old_mode=0o100644, new_mode=0o100755)
    plain = ChangedFile(path="src/a.py", old_path=None, status="modified")
    generated = ChangedFile(path="gen/api.pb.go", old_path=None, status="modified")
    packet = _packet(files=(symlink, submodule, executable, plain, generated))
    blobs = {"src/a.py": "x = 1\n", "gen/api.pb.go": "package gen\n", "run.sh": "#!/bin/sh\n", "link": "t\n"}
    units = derive_units(packet, blobs)

    stamped = stamp_answers(units, packet, marked_generated={"gen/api.pb.go"})
    by_path = {unit.path: (unit.answer_category, unit.category_rule) for unit in stamped}
    assert by_path["link"] == ("decision", "")
    assert by_path["vendor/lib"] == ("decision", "")
    assert by_path["run.sh"] == ("decision", "")
    assert by_path["src/a.py"] == ("quick_check", "")
    assert by_path["gen/api.pb.go"] == ("accepted_by_rule", "generated_files")
    # Unmarked, the same file is a quick check: nothing is accepted without evidence.
    again = stamp_answers(units, packet)
    assert {unit.path: unit.answer_category for unit in again}["gen/api.pb.go"] == "quick_check"
    # Pure: the same input gives the same stamps, and the units are otherwise untouched.
    assert stamp_answers(units, packet, marked_generated={"gen/api.pb.go"}) == stamped
    assert [replace(unit, answer_category="", category_rule="") for unit in stamped] == list(units)
