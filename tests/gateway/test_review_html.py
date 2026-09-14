"""The standalone HTML review view: self-contained, escaped, and navigable.

Three things can go wrong with this renderer and only one of them is cosmetic.
It can reach the network (then it is useless on the locked-down laptop it was
built for), it can emit a reviewer's diff as live markup (then reading a review
executes the change's own script tag), or it can quietly drop a file (then the
page is the only surface where a change can hide). Everything below is aimed at
those three.
"""

from __future__ import annotations

import json
import re
import webbrowser
from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review.html import render_html
from lemoncrow.pro.capabilities.review.models import (
    SCHEMA_VERSION,
    ChangedFile,
    ChangedSymbol,
    DiffHunk,
    EvidenceRecord,
    ImpactSite,
    ProvenanceRecord,
    ReviewOrderEntry,
    ReviewPacket,
)
from lemoncrow.pro.capabilities.review.render import render_review

_XSS = '<script>alert("1")</script>'


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reject ast-grep discovery before it can reach the managed download path.

    ``lc review`` runs the impact detectors, and ast-grep's managed bootstrap
    would try to *download* a binary into each throwaway repo. ``sg`` is the one
    candidate name ``_reject_reason`` refuses outright.
    """

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


@pytest.fixture(autouse=True)
def _no_review_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin `--open` to the static report for this file.

    Since PR-R3b, `--open` starts the loopback review workspace when a built
    frontend bundle is present and only falls back to the HTML report when
    there is none. A developer checkout *has* a bundle, so without this the
    tests below would spawn a real background server on the machine running
    them -- and they would be testing the workspace, which is not what this
    file is for.
    """

    from lemoncrow.pro.capabilities.review import workspace as workspace_mod

    monkeypatch.setattr(workspace_mod, "bundle_dir", lambda: None)


# --- packet fixtures --------------------------------------------------------


def _packet(
    *,
    files: tuple[ChangedFile, ...] | None = None,
    impact: tuple[ImpactSite, ...] = (),
    order: tuple[ReviewOrderEntry, ...] | None = None,
    provenance: ProvenanceRecord | None = None,
    evidence: tuple[EvidenceRecord, ...] = (),
    title: str = "fix(session): refresh() now takes an explicit context",
    degraded: tuple[str, ...] = (),
) -> ReviewPacket:
    resolved_files = _default_files() if files is None else files
    resolved_order = _default_order(resolved_files) if order is None else order
    return ReviewPacket(
        schema_version=SCHEMA_VERSION,
        generated_at="2026-09-08T09:14:02+00:00",
        repo_root="/home/dev/lemoncrow",
        range_mode="commit_range",
        base_rev="HEAD~1",
        head_rev="HEAD",
        base_sha="a" * 40,
        head_sha="b" * 40,
        title=title,
        files=resolved_files,
        symbols=(
            ChangedSymbol(
                symbol_name="refresh",
                qualified_name="session.refresh",
                kind="function",
                file_path="src/session/refresh.py",
                start_line=12,
                end_line=40,
                change="modified",
                caller_count=37,
                source="index",
            ),
        ),
        impact=impact,
        order=resolved_order,
        provenance=provenance if provenance is not None else ProvenanceRecord(),
        evidence=evidence,
        index_status="fresh",
        degraded=degraded,
        stats={
            "files": len(resolved_files),
            "additions": sum(f.additions for f in resolved_files),
            "deletions": sum(f.deletions for f in resolved_files),
            "hunks": sum(len(f.hunks) for f in resolved_files),
            "symbols": 1,
            "impact_sites": len(impact),
        },
    )


def _default_files() -> tuple[ChangedFile, ...]:
    return (
        ChangedFile(
            path="src/session/refresh.py",
            old_path=None,
            status="modified",
            additions=42,
            deletions=17,
            language="python",
            category="production",
            hunks=(
                DiffHunk(12, 8, 12, 14, "@@ -12,8 +12,14 @@ def refresh(session, *, context=None):", 9, 3),
                DiffHunk(88, 20, 94, 26, "@@ -88,20 +94,26 @@ class SessionRefresher:", 33, 14),
            ),
        ),
        ChangedFile(
            path="src/session/store.py",
            old_path=None,
            status="modified",
            additions=6,
            deletions=1,
            language="python",
            category="production",
            hunks=(DiffHunk(3, 4, 3, 9, f"@@ -3,4 +3,9 @@ {_XSS}", 6, 1),),
        ),
        ChangedFile(
            path="tests/test_refresh.py",
            old_path="tests/test_refresh_old.py",
            status="renamed",
            similarity=91,
            additions=18,
            deletions=0,
            language="python",
            category="test",
            hunks=(DiffHunk(1, 0, 1, 18, "@@ -1,0 +1,18 @@", 18, 0),),
        ),
        ChangedFile(path="assets/logo.png", old_path=None, status="added", is_binary=True, category="generated"),
    )


def _default_order(files: tuple[ChangedFile, ...]) -> tuple[ReviewOrderEntry, ...]:
    return tuple(
        ReviewOrderEntry(
            path=item.path,
            rank=rank,
            score=round(10.0 - rank, 4),
            reasons=(f"+{item.additions} -{item.deletions}", "public contract changed"),
            group=item.category,
        )
        for rank, item in enumerate(files, start=1)
    )


def _reaching_impact() -> tuple[ImpactSite, ...]:
    return (
        ImpactSite(
            kind="signature_change",
            path="src/adapters/mcp/session.py:L58",
            old="refresh(session)",
            new="now requires: context",
            snippet="_refresh_for(session)",
            in_patch=False,
            source_path="src/session/refresh.py",
        ),
        ImpactSite(
            kind="signature_change",
            path="src/sdk/middleware.py:L42",
            old="refresh(session)",
            new="now requires: context",
            snippet="self.refresh(session)",
            in_patch=False,
            source_path="src/session/refresh.py",
        ),
        ImpactSite(
            kind="removed_symbol",
            path="src/session/refresh.py:L120",
            old="_legacy_refresh",
            new="_legacy_refresh no longer defined here",
            snippet="def _legacy_refresh(session):",
            in_patch=True,
            source_path="src/session/refresh.py",
        ),
    )


# --- self-containment -------------------------------------------------------


def test_html_is_self_contained() -> None:
    page = render_html(_packet(impact=_reaching_impact()))

    assert "http://" not in page
    assert "https://" not in page
    assert "<script src=" not in page
    assert "<link " not in page
    assert "@import" not in page
    assert "fetch(" not in page
    assert "XMLHttpRequest" not in page
    assert "WebSocket" not in page
    # A remote font or image would arrive as one of these two.
    assert "@font-face" not in page
    assert "<img" not in page


def test_html_is_one_complete_document() -> None:
    page = render_html(_packet())

    assert page.startswith("<!doctype html>")
    assert page.rstrip().endswith("</html>")
    assert page.count("<style>") == 1
    assert page.count("<script>") == 1


def test_html_styles_both_colour_schemes() -> None:
    page = render_html(_packet())

    # The media query carries the OS preference; the attribute rules let the
    # in-page toggle override it in *both* directions.
    assert "@media(prefers-color-scheme:dark)" in page
    assert ":root[data-theme=dark]" in page
    assert ":root[data-theme=light]" in page


# --- escaping ---------------------------------------------------------------


def test_html_escapes_diff_content() -> None:
    page = render_html(_packet())

    assert _XSS not in page
    assert "&lt;script&gt;alert(&quot;1&quot;)&lt;/script&gt;" in page


def test_html_escapes_every_untrusted_field() -> None:
    hostile = _XSS
    packet = _packet(
        title=hostile,
        impact=(
            ImpactSite(
                kind="contract_literal",
                path=f"src/{hostile}.py:L1",
                old=hostile,
                new=hostile,
                snippet=hostile,
                in_patch=False,
                source_path=hostile,
            ),
        ),
        provenance=ProvenanceRecord(
            status="matched",
            host=hostile,
            model=hostile,
            session_id=hostile,
            task=hostile,
            commands_run=(hostile,),
            subagents=((hostile, 1),),
            uninspected_impacted=(hostile,),
            match_confidence=1.0,
            match_reason=hostile,
        ),
        evidence=(EvidenceRecord(name=hostile, status="FAIL", detail=hostile, source=hostile),),
        degraded=(hostile,),
    )

    page = render_html(packet)

    assert "<script>alert" not in page
    # ...and the payload is still *there*, just inert.
    assert page.count("&lt;script&gt;") >= 10


def test_packet_json_block_cannot_close_its_own_script_tag() -> None:
    page = render_html(_packet())

    # The embedded packet is escaped text inside <pre>, not a live script, so a
    # "</script>" hiding in a diff cannot end the document early.
    assert page.count("</script>") == 1
    # The JSON is escaped like every other untrusted string, quotes included.
    assert "&quot;schema_version&quot;: 2" in page


# --- navigation -------------------------------------------------------------


def test_html_includes_every_ordered_file() -> None:
    packet = _packet()
    page = render_html(packet)

    for entry in packet.order:
        assert entry.path in page, entry.path


def test_a_file_the_ranker_dropped_is_still_in_the_document() -> None:
    """``--limit`` truncates the order; it must not truncate the review."""

    files = _default_files()
    packet = _packet(files=files, order=_default_order(files)[:1])

    page = render_html(packet)

    for item in files:
        assert item.path in page, item.path


def test_every_jump_link_resolves_to_an_element_on_the_page() -> None:
    page = render_html(_packet(impact=_reaching_impact()))

    targets = set(re.findall(r'id="([^"]+)"', page))
    hrefs = {href for href in re.findall(r'href="#([^"]+)"', page)}
    assert hrefs, "the page has no internal navigation at all"
    assert hrefs <= targets, hrefs - targets


def test_file_anchors_are_positional_not_path_derived() -> None:
    """A path is user data; user data does not belong in an ``id``."""

    page = render_html(_packet())

    ids = re.findall(r'<section class="file" id="([^"]+)"', page)
    assert ids == ["f1", "f2", "f3", "f4"]


def test_navigation_lists_files_in_review_order() -> None:
    page = render_html(_packet())

    listed = re.findall(r'<li data-path="([^"]+)"', page)
    assert listed == ["src/session/refresh.py", "src/session/store.py", "tests/test_refresh.py", "assets/logo.png"]


def test_wide_content_scrolls_inside_its_own_container() -> None:
    page = render_html(_packet())

    assert ".scroll{overflow-x:auto}" in page
    assert page.count('<div class="scroll">') >= 3


# --- content parity with the terminal ---------------------------------------


def test_html_and_terminal_report_the_same_findings() -> None:
    packet = _packet(impact=_reaching_impact())

    page = render_html(packet)
    text = render_review(packet, no_color=True)

    assert "refresh(session) now requires: context" in text
    assert "refresh(session) now requires: context" in page
    # One finding, two locations -- in both renderings.
    assert page.count("refresh(session) now requires: context") == 1
    for site in ("src/adapters/mcp/session.py:L58", "src/sdk/middleware.py:L42"):
        assert site in page
        assert site in text


def test_a_qualified_finding_is_qualified_in_both_renderings() -> None:
    """The page must not be the one surface where the doubt gets dropped.

    A method call site the detector could not attribute to the changed class ships
    with the reason attached. If the HTML printed it under the confident heading
    the two renderings would disagree about what is a fact -- which this module's
    whole parity rule exists to prevent.
    """

    doubt = "unverified: at least 4 definitions named refresh() in this repo"
    packet = _packet(
        impact=tuple(
            replace(site, old="SessionRefresher.refresh(...)", uncertainty=doubt)
            for site in _reaching_impact()
            if site.kind == "signature_change"
        )
    )

    page = render_html(packet)
    text = render_review(packet, no_color=True)

    for rendering in (page, text):
        assert doubt in rendering
        assert "possible call sites:" in rendering
        assert "untouched call sites:" not in rendering
    # The terminal drops the warning glyph; the page drops the warning styling.
    assert "⚠" not in text
    assert 'class="finding unsure"' in page


def test_in_patch_sites_are_kept_but_demoted_behind_a_disclosure() -> None:
    packet = _packet(impact=_reaching_impact())

    page = render_html(packet)
    text = render_review(packet, no_color=True)

    # The terminal drops in-patch sites entirely; the page keeps them folded.
    assert "_legacy_refresh no longer defined here" not in text
    assert "_legacy_refresh no longer defined here" in page
    assert "1 impacted site inside the patch" in page
    # ...and the out-of-patch findings still come first.
    assert page.index("now requires: context") < page.index("1 impacted site inside the patch")


def test_hunk_headers_and_counts_are_rendered_per_file() -> None:
    page = render_html(_packet())

    assert "@@ -88,20 +94,26 @@ class SessionRefresher:" in page
    assert "L94-119" in page
    assert ">+33<" in page and ">-14<" in page
    assert "binary file" in page


def test_provenance_and_evidence_panes_report_gaps_as_gaps() -> None:
    packet = _packet(
        provenance=ProvenanceRecord(status="unknown", match_reason="no session overlapped this range"),
        evidence=(EvidenceRecord(name="Full suite", status="NOT_RUN", detail="", source="none"),),
    )

    page = render_html(packet)

    assert "unknown" in page
    assert "no session overlapped this range" in page
    assert "NOT_RUN" in page
    assert "Generated with" in page


def test_agent_reads_that_were_never_recorded_are_not_reported_as_zero() -> None:
    packet = _packet(
        provenance=ProvenanceRecord(
            status="matched",
            host="codex",
            model="gpt-x",
            session_id="abc123",
            files_inspected=(),
            match_confidence=0.9,
            match_reason="workspace path",
        )
    )

    page = render_html(packet)

    assert "not recorded for this host" in page
    assert "0 files" not in page


def test_the_uninspected_list_is_not_truncated_the_way_the_terminal_truncates_it() -> None:
    paths = tuple(f"src/mod_{index:02d}.py" for index in range(14))
    packet = _packet(
        provenance=ProvenanceRecord(
            status="matched",
            host="claude",
            session_id="abc123",
            files_inspected=("src/session/refresh.py",),
            uninspected_impacted=paths,
            match_confidence=0.9,
            match_reason="workspace path",
        )
    )

    page = render_html(packet)
    text = render_review(packet, no_color=True)

    assert "and 4 more" in text
    for path in paths:
        assert path in page, path


def test_footer_states_the_index_status_the_degradations_and_the_verdict_ban() -> None:
    packet = _packet(degraded=("centrality", "provenance_ambiguous"))

    page = render_html(packet)

    assert "Human review  REQUIRED" in page
    assert "index: fresh" in page
    assert "centrality" in page
    assert "provenance_ambiguous" in page


def test_an_empty_range_still_renders_a_whole_document() -> None:
    packet = _packet(files=(), order=())

    page = render_html(packet)

    assert page.startswith("<!doctype html>")
    assert "no changes in this range" in page
    assert "Human review  REQUIRED" in page


# --- CLI --------------------------------------------------------------------


def _signature(offset: int) -> pygit2.Signature:
    return pygit2.Signature("Fixture Tester", "fixture@example.com", 1700000000 + offset, 0)


def _commit(repo: Any, message: str, offset: int) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = _signature(offset)
    return str(repo.create_commit("HEAD", signature, signature, message, tree, parents))


def _fixture_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.com"

    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    _commit(repo, "seed", 0)

    (root / "src" / "app.py").write_text("def one():\n    return 2\n", encoding="utf-8")
    _commit(repo, "change the return value", 60)
    return root


def _invoke(tmp_path: Path, args: list[str]) -> Any:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(tmp_path / "store"), "review", *args], catch_exceptions=False)


def test_cli_html_flag_writes_file(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    out = tmp_path / "reports" / "review.html"

    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--no-color", "--html", str(out)])

    assert result.exit_code == 0, result.output
    assert out.exists(), "the parent directory should have been created"
    page = out.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert "src/app.py" in page
    assert str(out) in result.output


def test_cli_html_and_json_keep_stdout_parseable(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    out = tmp_path / "review.html"

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--root",
            str(tmp_path / "store"),
            "review",
            "--repo-root",
            str(repo_root),
            "HEAD~1",
            "--json",
            "--html",
            str(out),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert out.exists()
    # The report path is progress output, so it must not land in the payload.
    assert json.loads(result.stdout)["schema_version"] == SCHEMA_VERSION


def test_cli_html_reports_an_unwritable_path_without_a_traceback(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "HEAD~1", "--no-color", "--html", str(blocker / "out.html")],
    )

    assert result.exit_code != 0
    assert "cannot write" in result.output
    assert "Traceback" not in result.output


def test_cli_open_flag_never_raises_headless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = _fixture_repo(tmp_path)
    out = tmp_path / "review.html"

    def boom(url: str, *args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("no browser on this box")

    monkeypatch.setattr(webbrowser, "open", boom)

    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "HEAD~1", "--no-color", "--html", str(out), "--open"],
    )

    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "could not open a browser" in result.output


def test_cli_open_without_a_bundle_still_writes_a_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no frontend bundle, `--open` degrades to the static report.

    This is the pip-install path: no bundle was ever built, so the workspace
    cannot be served. Saying so and opening the report beats refusing to run.
    """

    repo_root = _fixture_repo(tmp_path)
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)

    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--no-color", "--open"])

    assert result.exit_code == 0, result.output
    written = sorted((tmp_path / "store" / "review").glob("*.html"))
    assert len(written) == 1, written
    assert opened and opened[0].startswith("file://")
    assert written[0].name.startswith("review-")
    assert "no built frontend bundle found" in result.stderr


def test_cli_review_html_is_stable_across_runs(tmp_path: Path) -> None:
    """Same range, same document: a diffable artefact beats a fresh one."""

    repo_root = _fixture_repo(tmp_path)
    first = tmp_path / "a.html"
    second = tmp_path / "b.html"

    for out in (first, second):
        result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--no-color", "--html", str(out)])
        assert result.exit_code == 0, result.output

    stamp = re.compile(r"generated_at&quot;: &quot;[^&]+|generated [0-9T:+.-]+")
    assert stamp.sub("", first.read_text(encoding="utf-8")) == stamp.sub("", second.read_text(encoding="utf-8"))
