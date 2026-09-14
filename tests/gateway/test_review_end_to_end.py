"""``lc review`` end to end: a real repo in, a rendered review and JSON out.

The other review test modules each pin one producer in isolation. This one is
deliberately integrative and deliberately unmocked: it builds a git repository
where a function gains a required parameter while a second, *untouched* file
still calls it the old way, then asserts that the finished product surfaces that
caller — through the packet, through the terminal ``ATTENTION`` section, and
through ``--json``. Those are the three things a user actually consumes, and
each of them has broken independently while every unit test stayed green.

No index is built, so this also pins the honest floor: the whole chain has to
work when the only structural signal available is tree-sitter over the diff.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.models import ImpactSite, ReviewPacket
from lemoncrow.pro.capabilities.review.packet import build_review_packet
from lemoncrow.pro.capabilities.review.render import render_review


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reject ast-grep discovery before it can reach the managed download path.

    The impact detectors bootstrap ast-grep on demand, which would download a
    binary into every throwaway repo. ``sg`` is the one candidate name
    ``_reject_reason`` refuses outright, so discovery reports "unavailable"
    without attempting a fetch — and the review still has to work.
    """

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _signature(offset: int) -> pygit2.Signature:
    return pygit2.Signature("Fixture Tester", "fixture@example.com", 1700000000 + offset, 0)


def _commit(repo: Any, message: str, offset: int) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    sig = _signature(offset)
    return str(repo.create_commit("HEAD", sig, sig, message, tree, parents))


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _repo_with_a_signature_change(tmp_path: Path) -> Path:
    """``refresh(user)`` gains a required ``context``; ``jobs/`` still calls it."""

    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.com"

    _write(root, "src/auth.py", "def refresh(user):\n    return user\n")
    _write(
        root,
        "jobs/session_cleanup.py",
        "from src.auth import refresh\n\n\ndef sweep(user):\n    return refresh(user)\n",
    )
    _write(root, "README.md", "# fixture\n")
    _commit(repo, "seed", 0)

    _write(root, "src/auth.py", "def refresh(user, context):\n    return (user, context)\n")
    _write(root, "tests/test_auth.py", "def test_refresh():\n    assert True\n")
    _write(root, "README.md", "# fixture\n\nrefresh now takes a context.\n")
    _commit(repo, "refresh() now requires context", 60)
    return root


_SESSION_MANAGER = (
    "class SessionManager:\n"
    "    def __init__(self, store):\n"
    "        self.store = store\n"
    "\n"
    "    def refresh(self, user{extra}):\n"
    "        return self.store.rotate(user)\n"
)
_MANAGER_CALLER = (
    "from src.auth.session import SessionManager\n"
    "\n"
    "\n"
    "def {fn}(store, user):\n"
    "    manager = SessionManager(store)\n"
    "    return manager.refresh(user)\n"
)


def _repo_with_a_method_signature_change(tmp_path: Path, *, rival_class: bool = False) -> Path:
    """The plan's own §4.1 demo, built literally.

    ``SessionManager.refresh(user)`` gains a required ``context`` while
    ``jobs/session_cleanup.py`` and ``workers/token_refresh.py`` still call it the
    old way. This is the marquee example and it produced *nothing* -- both the
    ast-grep pattern and the text gate demanded a bare ``refresh(`` call, which a
    method never is -- while the module-level fixture above worked. Python is
    method-heavy, so the shape the product could not see was the common one.

    ``rival_class`` adds a second, unrelated class that also defines ``refresh``,
    which is the case where ``manager.refresh(user)`` genuinely cannot be pinned to
    ``SessionManager`` without type inference.
    """

    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.com"

    _write(root, "src/auth/session.py", _SESSION_MANAGER.format(extra=""))
    _write(root, "jobs/session_cleanup.py", _MANAGER_CALLER.format(fn="sweep"))
    _write(root, "workers/token_refresh.py", _MANAGER_CALLER.format(fn="rotate_all"))
    _write(root, "README.md", "# fixture\n")
    if rival_class:
        _write(root, "src/cache/tokens.py", "class TokenCache:\n    def refresh(self, key):\n        return key\n")
    _commit(repo, "seed", 0)

    _write(root, "src/auth/session.py", _SESSION_MANAGER.format(extra=", context"))
    _commit(repo, "SessionManager.refresh() now requires context", 60)
    return root


def _packet(root: Path, store_root: Path) -> ReviewPacket:
    return build_review_packet(root, resolve_rev_range(root, "HEAD~1"), store_root=store_root)


def _attention_of(text: str) -> str:
    """The ATTENTION section alone. It runs from its heading to ``START HERE``.

    Bounding it matters: ATTENTION is the *first* section now, so slicing to the
    end of the output would sweep the review order and the evidence block in with
    it and quietly pass assertions about what the findings do or do not say.
    """

    assert "ATTENTION" in text, text
    return text.split("ATTENTION", 1)[1].split("START HERE", 1)[0]


def _attention(root: Path, store_root: Path) -> str:
    text = render_review(
        build_review_packet(root, resolve_rev_range(root, "HEAD~1"), store_root=store_root), no_color=True
    )
    return _attention_of(text)


def _invoke(tmp_path: Path, args: list[str]) -> Any:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(tmp_path / "store"), "review", *args], catch_exceptions=False)


# --- the packet -------------------------------------------------------------


def test_packet_reaches_the_untouched_caller(tmp_path: Path) -> None:
    packet = _packet(_repo_with_a_signature_change(tmp_path), tmp_path / "store")

    reaching = [site for site in packet.impact if not site.in_patch]
    assert reaching, f"nothing reached outside the patch; degraded={packet.degraded!r}"

    callers = [site for site in reaching if site.path.startswith("jobs/session_cleanup.py:L")]
    assert callers, f"the untouched caller is missing from {[s.path for s in reaching]!r}"
    assert any(site.kind == "signature_change" for site in callers)
    assert any("context" in (site.new or "") for site in callers)
    # No provenance matched, so nothing may be claimed about what was opened.
    assert all(site.inspected_by_agent is None for site in callers)

    assert packet.stats["impact_sites"] == len(packet.impact)
    assert any(symbol.symbol_name == "refresh" for symbol in packet.symbols)


def test_the_changed_contract_is_ranked_first_and_says_why(tmp_path: Path) -> None:
    packet = _packet(_repo_with_a_signature_change(tmp_path), tmp_path / "store")

    assert packet.order[0].path == "src/auth.py"
    assert "public contract changed" in packet.order[0].reasons
    assert "1 untouched impacted site(s)" in packet.order[0].reasons
    # Every promoted file explains itself, and the prose file sinks to the end.
    assert all(entry.reasons for entry in packet.order if entry.score > 2.0)
    assert packet.order[-1].path == "README.md"
    assert [entry.rank for entry in packet.order] == [1, 2, 3]


def test_no_impact_still_produces_a_whole_packet(tmp_path: Path) -> None:
    root = _repo_with_a_signature_change(tmp_path)
    packet = build_review_packet(
        root,
        resolve_rev_range(root, "HEAD~1"),
        store_root=tmp_path / "store",
        with_impact=False,
    )

    assert packet.impact == ()
    assert packet.symbols == ()
    assert "impact_disabled" in packet.degraded
    assert packet.files, "the diff is independent of the impact analysis"
    assert [entry.rank for entry in packet.order] == list(range(1, len(packet.files) + 1))

    text = render_review(packet, no_color=True)
    assert "ATTENTION" not in text
    # The index was never consulted, so the footer must not report it missing.
    assert "index:" not in text
    assert "impact_disabled" in text


# --- the rendering ----------------------------------------------------------


def test_attention_names_the_untouched_call_site(tmp_path: Path) -> None:
    packet = _packet(_repo_with_a_signature_change(tmp_path), tmp_path / "store")
    text = render_review(packet, no_color=True)

    attention = _attention_of(text)
    assert "refresh" in attention
    assert "untouched call sites:" in attention
    assert "jobs/session_cleanup.py:L" in attention
    # The patch's own file is never listed as something the change "reaches".
    assert "src/auth.py:L" not in attention


def test_a_method_signature_change_is_visible_at_all(tmp_path: Path) -> None:
    """Plan §4.1's own example, which used to render an empty ATTENTION section.

    The bar is the plan's target text: name the changed contract and both untouched
    call sites. The product exceeds it in one way it is allowed to -- the headline
    carries the class, because ``refresh()`` alone is not a contract a reader can
    look up in a method-heavy codebase.
    """

    attention = _attention(_repo_with_a_method_signature_change(tmp_path), tmp_path / "store")

    assert "SessionManager.refresh(...) now requires: context" in attention, attention
    assert "untouched call sites:" in attention
    assert "jobs/session_cleanup.py:L6" in attention
    assert "workers/token_refresh.py:L6" in attention
    # One finding, not one per site, and stated as fact -- nothing here is unproven.
    assert attention.count("⚠") == 1
    assert "unverified" not in attention
    # It is traceable back to the edit that caused it.
    assert "(src/auth/session.py)" in attention


def test_an_ambiguous_method_is_qualified_not_dropped_and_not_asserted(tmp_path: Path) -> None:
    """A second class defining ``refresh`` makes the receiver unknowable.

    Both wrong answers are pinned here. Dropping the finding hides a real defect;
    printing it under "untouched call sites" claims these calls reach
    ``SessionManager``, which nothing proved. The finding ships, marked, with the
    ambiguity spelled out and the count that caused it.
    """

    root = _repo_with_a_method_signature_change(tmp_path, rival_class=True)
    packet = _packet(root, tmp_path / "store")
    attention = _attention(root, tmp_path / "store")

    assert "SessionManager.refresh(...) now requires: context" in attention, attention
    assert "jobs/session_cleanup.py:L6" in attention
    assert "at least 2 definitions named refresh() in this repo" in attention
    assert "may belong to another class" in attention
    # Not dressed up as a proven warning, and not labelled as confirmed call sites.
    assert "⚠" not in attention
    assert "possible call sites:" in attention
    assert "untouched call sites:" not in attention

    sites = [site for site in packet.impact if site.kind == "signature_change"]
    assert sites and all(site.uncertainty for site in sites)
    # The ranking must not restate a maybe as a count of facts.
    reasons = packet.order[0].reasons
    assert any("unverified impacted site(s)" in reason for reason in reasons), reasons
    assert not any("untouched impacted site(s)" in reason for reason in reasons), reasons


def test_the_module_level_case_is_unchanged_by_method_support(tmp_path: Path) -> None:
    """The shape that already worked must keep working, spelled exactly as before."""

    attention = _attention(_repo_with_a_signature_change(tmp_path), tmp_path / "store")

    assert "⚠ refresh(...) now requires: context" in attention, attention
    assert "untouched call sites:" in attention
    assert "jobs/session_cleanup.py:L5" in attention
    assert "unverified" not in attention


def test_rich_rendering_surfaces_the_same_finding(tmp_path: Path) -> None:
    packet = _packet(_repo_with_a_signature_change(tmp_path), tmp_path / "store")
    text = render_review(packet, no_color=False)

    assert "ATTENTION" in text
    assert "jobs/session_cleanup.py" in text
    assert "Human review  REQUIRED" in text


def test_one_finding_lists_its_sites_once_not_once_per_site(tmp_path: Path) -> None:
    """A finding that reaches many files is one warning, not many.

    Rendering per-site is what made a real run of this command emit forty
    near-identical warning lines; the grouping is the fix and this pins it.
    """

    root = tmp_path / "fanout"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.com"
    _write(root, "src/auth.py", "def refresh(user):\n    return user\n")
    for index in range(4):
        _write(
            root,
            f"jobs/job_{index}.py",
            f"from src.auth import refresh\n\n\ndef sweep_{index}(user):\n    return refresh(user)\n",
        )
    _commit(repo, "seed", 0)
    _write(root, "src/auth.py", "def refresh(user, context):\n    return (user, context)\n")
    _commit(repo, "refresh() now requires context", 60)

    attention = _attention_of(render_review(_packet(root, tmp_path / "store"), no_color=True))

    assert attention.count("⚠") == 1, attention
    assert attention.count("untouched call sites:") == 1
    assert sum(f"jobs/job_{index}.py:L" in attention for index in range(4)) >= 2


def test_execution_evidence_says_unknown_rather_than_guessing(tmp_path: Path) -> None:
    packet = _packet(_repo_with_a_signature_change(tmp_path), tmp_path / "store")
    text = render_review(packet, no_color=True)

    assert "EXECUTION EVIDENCE" in text
    evidence = text.split("EXECUTION EVIDENCE", 1)[1]
    assert "Generated with: unknown" in evidence
    # An unknown attribution still states why it is unknown.
    assert packet.provenance.match_reason in evidence
    assert "PASS" not in evidence
    assert text.rstrip().splitlines()[-1].startswith("index: absent")


def test_start_here_names_one_file_and_carries_its_facts_and_reasons(tmp_path: Path) -> None:
    packet = _packet(_repo_with_a_signature_change(tmp_path), tmp_path / "store")
    text = render_review(packet, no_color=True)

    start = text.split("START HERE", 1)[1].split("THEN", 1)[0]
    assert "1. src/auth.py" in start
    assert "modified · production · +2 -2" in start
    assert "- public contract changed" in start
    # The churn is stated once, on the fact line -- never repeated as a bullet.
    assert "- +2 -2" not in start
    # Exactly one file is promoted; the rest are one-liners under THEN.
    assert start.count(". ") == 1, start


def test_the_rest_of_the_order_is_one_line_each(tmp_path: Path) -> None:
    """A ranking a reviewer has to scroll through is a ranking they do not read."""

    packet = _packet(_repo_with_a_signature_change(tmp_path), tmp_path / "store")
    then = render_review(packet, no_color=True).split("THEN", 1)[1].split("EXECUTION EVIDENCE", 1)[0]

    lines = [line for line in then.splitlines() if line.strip()]
    assert len(lines) == len(packet.order) - 1, then
    assert all(line.startswith("  ") and ". " in line for line in lines), then
    # The delta rides the same line as the path, so the entry is genuinely one line.
    assert any("README.md  +2 -0" in line for line in lines), then


# --- the caps ---------------------------------------------------------------


_FANOUT_CONTRACTS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")


def _repo_with_six_findings(tmp_path: Path) -> Path:
    """Six changed contracts in one file, each with its own untouched caller."""

    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.com"

    _write(root, "src/auth.py", "".join(f"def {n}(user):\n    return user\n\n\n" for n in _FANOUT_CONTRACTS))
    for name in _FANOUT_CONTRACTS:
        _write(
            root,
            f"jobs/{name}.py",
            f"from src.auth import {name}\n\n\ndef sweep_{name}(user):\n    return {name}(user)\n",
        )
    _commit(repo, "seed", 0)

    _write(
        root,
        "src/auth.py",
        "".join(f"def {n}(user, context):\n    return (user, context)\n\n\n" for n in _FANOUT_CONTRACTS),
    )
    _commit(repo, "every contract now requires context", 60)
    return root


def test_attention_shows_three_findings_by_default_and_all_of_them_under_all(tmp_path: Path) -> None:
    """Six findings do not fit the five seconds a reviewer spends on this screen.

    The cap is only defensible because it says what it withheld and names the
    flag that undoes it: an elision the reader cannot tell from "nothing else was
    found" is worse than the scrolling it saves.
    """

    packet = _packet(_repo_with_six_findings(tmp_path), tmp_path / "store")

    default = render_review(packet, no_color=True)
    every = render_review(packet, no_color=True, show_all=True)

    found = every.count("⚠")
    assert found == len(_FANOUT_CONTRACTS), every
    assert default.count("⚠") == 3, default
    assert f"… and {found - 3} more (lc review --all)" in default
    assert "(lc review --all)" not in every
    # What survives the cap is the most severe, never a subset chosen at random:
    # the withheld findings are all still there when the cap is lifted.
    for name in _FANOUT_CONTRACTS:
        assert f"{name}(...) now requires: context" in every, name


def test_a_finding_lists_five_locations_by_default_and_counts_the_rest(tmp_path: Path) -> None:
    """One contract can reach a lot of files; five is enough to judge the radius."""

    base = _packet(_repo_with_a_signature_change(tmp_path), tmp_path / "store")
    packet = replace(
        base,
        impact=tuple(
            ImpactSite(
                kind="signature_change",
                path=f"jobs/job_{index:02d}.py:L{index + 4}",
                old="refresh(...)",
                new="now requires: context",
                snippet="return refresh(user)",
                source_path="src/auth.py",
            )
            for index in range(9)
        ),
    )

    default = render_review(packet, no_color=True)
    every = render_review(packet, no_color=True, show_all=True)

    assert sum(1 for line in default.splitlines() if "jobs/job_" in line) == 5, default
    assert "… and 4 more" in default
    assert sum(1 for line in every.splitlines() if "jobs/job_" in line) == 9, every
    assert "… and 4 more" not in every


def test_the_file_table_is_behind_all_and_the_findings_are_not(tmp_path: Path) -> None:
    """``FILES`` is a ``git diff --stat`` reprint; it is not why anyone runs this."""

    packet = _packet(_repo_with_a_signature_change(tmp_path), tmp_path / "store")

    default = render_review(packet, no_color=True)
    every = render_review(packet, no_color=True, show_all=True)

    assert "\nFILES\n" not in default
    assert "\nFILES\n" in every
    # ...and the risks are above the fold in the default view.
    assert default.index("ATTENTION") < default.index("START HERE")
    assert default.splitlines().index("ATTENTION") < 10, default


# --- the CLI ----------------------------------------------------------------


def test_cli_json_round_trips_the_whole_packet(tmp_path: Path) -> None:
    root = _repo_with_a_signature_change(tmp_path)

    result = _invoke(tmp_path, ["--repo-root", str(root), "HEAD~1", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    expected = json.loads(json.dumps(_packet(root, tmp_path / "store").to_dict(), default=str))
    payload.pop("generated_at")
    expected.pop("generated_at")
    assert payload == expected, "two builds of the same range must produce the same packet"

    assert payload["schema_version"] == 2
    assert payload["dirty"] is False, "reviewing a repo must not leave it looking dirty"
    site_paths = [site["path"] for site in payload["impact"]]
    assert any(path.startswith("jobs/session_cleanup.py:L") for path in site_paths), site_paths
    assert payload["order"][0]["path"] == "src/auth.py"
    assert payload["provenance"]["status"] == "unknown"


def test_json_is_never_capped_the_way_the_terminal_is(tmp_path: Path) -> None:
    """A machine consumer handed three of six findings is the reader who cannot tell.

    The caps exist because a human scans a screen for five seconds. ``--json`` has
    no screen and no scanner, so the payload keeps every finding and every site --
    a hook that gates a commit on this must see what the packet actually found.
    """

    root = _repo_with_six_findings(tmp_path)
    text = render_review(_packet(root, tmp_path / "store"), no_color=True)

    result = _invoke(tmp_path, ["--repo-root", str(root), "HEAD~1", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    reaching = [site for site in payload["impact"] if not site["in_patch"]]
    assert {site["old"] for site in reaching} == {f"{name}(...)" for name in _FANOUT_CONTRACTS}, reaching
    assert payload["stats"]["impact_sites"] == len(payload["impact"])
    # ...while the terminal view of the same range shows three and says so.
    assert text.count("⚠") == 3, text
    assert "(lc review --all)" in text


def test_cli_text_output_matches_the_packet(tmp_path: Path) -> None:
    root = _repo_with_a_signature_change(tmp_path)

    result = _invoke(tmp_path, ["--repo-root", str(root), "HEAD~1", "--no-color"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == render_review(_packet(root, tmp_path / "store"), no_color=True).strip()


def test_running_review_twice_reviews_the_same_range(tmp_path: Path) -> None:
    """The impact pass writes a code index into the repo; that is not user work.

    Without the store-directory exemption the second invocation sees four
    untracked sqlite files, calls the tree dirty, and silently switches from
    ``HEAD~1..HEAD`` to a working-tree review.
    """

    root = _repo_with_a_signature_change(tmp_path)

    first = _invoke(tmp_path, ["--repo-root", str(root), "--json"])
    second = _invoke(tmp_path, ["--repo-root", str(root), "--json"])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output

    first_payload = json.loads(first.output)
    second_payload = json.loads(second.output)
    assert first_payload["range_mode"] == second_payload["range_mode"] == "commit_range"
    assert first_payload["dirty"] is second_payload["dirty"] is False
    assert [item["path"] for item in first_payload["files"]] == [item["path"] for item in second_payload["files"]]
