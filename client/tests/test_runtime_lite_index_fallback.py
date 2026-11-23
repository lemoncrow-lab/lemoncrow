"""code_search answers from the files when the index cannot, and records why."""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client import cli
from lemoncrow_client.kit import search as kit_search
from lemoncrow_client.lite import mcp as lite_mcp
from lemoncrow_client.lite.index import LiteIndex
from lemoncrow_client.lite.mcp import LiteMcp
from lemoncrow_client.localtools import search as local_search
from lemoncrow_client.trace import summarize_code_search

NEEDLE = "unique_needle_symbol"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _env(root: Path, tmp_path: Path) -> dict[str, str]:
    return {"LEMONCROW_WORKSPACE_ROOT": str(root), "LEMONCROW_HOME": str(tmp_path / "state"), "HOME": str(tmp_path)}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "lite@example.invalid")
    _git(root, "config", "user.name", "Runtime Lite")
    (root / "src" / "app.py").write_text(f"def {NEEDLE}():\n    return 1\n", encoding="utf-8")
    (root / "docs" / "guide.md").write_text("nothing relevant here\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def server(repo: Path, tmp_path: Path) -> Iterator[LiteMcp]:
    instance = LiteMcp(environment=_env(repo, tmp_path))
    try:
        yield instance
    finally:
        instance.close()


def _text(result: dict[str, Any]) -> str:
    return "\n".join(str(block.get("text") or "") for block in result["content"])


def _stats(tmp_path: Path) -> dict[str, Any]:
    return summarize_code_search(tmp_path / "state" / "runtime" / "traces")


def _outcomes(tmp_path: Path) -> list[dict[str, Any]]:
    traces = tmp_path / "state" / "runtime" / "traces"
    records = [json.loads(line) for path in traces.glob("*/client-*.jsonl") for line in path.read_text().splitlines()]
    return [record["outcome"] for record in records if record["tool"] == "code_search"]


def _break_schema(repo: Path) -> None:
    db = LiteIndex(repo).db_path
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO meta VALUES('schema_version','999')")


def _no_hits(_self: LiteIndex, query: str, **_kwargs: object) -> dict[str, Any]:
    return {"query": query, "files": [], "candidate_files": [], "related_symbols": [], "lite": True}


def test_a_healthy_index_answers_with_no_fallback(server: LiteMcp, tmp_path: Path) -> None:
    server.prepare()
    result = server.call("code_search", {"query": NEEDLE})
    assert result["isError"] is False
    assert "index_fallback" not in _text(result)
    assert result["structuredContent"]["files"][0]["path"] == "src/app.py"
    assert "_lc_fallback" not in result
    assert _outcomes(tmp_path)[0]["degraded"] is False
    assert _stats(tmp_path)["indexed"] == 1


def test_a_newer_index_schema_answers_by_grep_and_keeps_the_remedy(repo: Path, tmp_path: Path) -> None:
    _break_schema(repo)
    instance = LiteMcp(environment=_env(repo, tmp_path))
    try:
        result = instance.call("code_search", {"query": NEEDLE})
        assert result["isError"] is False
        assert _text(result).startswith("[lc:index_fallback reason=index_upgrade_required]")
        assert "restart the coding-host session" in _text(result)
        assert "src/app.py" in _text(result)
        assert result["structuredContent"]["mode"] == "grep"
        assert result["structuredContent"]["matches"] == 1
        outcome = _outcomes(tmp_path)[0]
        assert outcome["degraded"] is True
        assert outcome["degraded_reason"] == "index_upgrade_required"
        # The tools with no grep equivalent still say what is wrong.
        assert _text(instance.call("relations", {"symbol": NEEDLE})).startswith("[lc:upgrade_required]")
    finally:
        instance.close()


def test_a_failed_search_answers_by_grep_and_names_the_failure(
    server: LiteMcp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("disk vanished")

    monkeypatch.setattr(LiteIndex, "search", fail)
    result = server.call("code_search", {"query": NEEDLE})
    assert result["isError"] is False
    assert _text(result).startswith("[lc:index_fallback reason=index_failed]")
    assert "disk vanished" in _text(result)
    assert "src/app.py" in _text(result)
    assert _stats(tmp_path)["grep_fallback_reasons"] == {"index_failed": 1}


def test_a_corrupt_index_answers_by_grep(server: LiteMcp, monkeypatch: pytest.MonkeyPatch) -> None:
    def corrupt(*_args: object, **_kwargs: object) -> object:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(LiteIndex, "search", corrupt)
    result = server.call("code_search", {"query": NEEDLE})
    assert result["isError"] is False
    assert "reason=index_failed" in _text(result)
    assert "src/app.py" in _text(result)


def test_a_build_in_progress_answers_by_grep(server: LiteMcp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    release = threading.Event()
    monkeypatch.setattr(lite_mcp, "_INDEX_WAIT_S", 0.01)
    thread = threading.Thread(target=release.wait, daemon=True)
    server._prepare_thread = thread
    thread.start()
    try:
        result = server.call("code_search", {"query": NEEDLE})
    finally:
        release.set()
        thread.join()
    assert result["isError"] is False
    assert _text(result).startswith("[lc:index_fallback reason=index_building]")
    assert "src/app.py" in _text(result)
    assert _stats(tmp_path)["grep_fallback_reasons"] == {"index_building": 1}


def test_an_empty_index_answers_by_grep(server: LiteMcp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server.prepare()
    with sqlite3.connect(server.index.db_path) as connection:
        connection.execute("DELETE FROM files")
    monkeypatch.setattr(LiteIndex, "search", _no_hits)
    result = server.call("code_search", {"query": NEEDLE})
    assert result["isError"] is False
    assert _text(result).startswith("[lc:index_fallback reason=index_empty]")
    assert "src/app.py" in _text(result)
    assert _stats(tmp_path)["grep_fallback_reasons"] == {"index_empty": 1}


def test_emptiness_is_read_from_the_index(repo: Path) -> None:
    index = LiteIndex(repo)
    assert index.is_empty() is True
    index.prepare()
    assert index.is_empty() is False


def test_a_miss_in_a_healthy_index_is_checked_against_the_files(
    server: LiteMcp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server.prepare()
    monkeypatch.setattr(LiteIndex, "search", _no_hits)
    result = server.call("code_search", {"query": NEEDLE})
    assert result["isError"] is False
    assert _text(result).startswith("[lc:index_fallback reason=no_index_matches]")
    assert "src/app.py" in _text(result)
    assert _stats(tmp_path)["grep_fallback_reasons"] == {"no_index_matches": 1}


def test_a_real_miss_says_so_without_an_error(server: LiteMcp) -> None:
    server.prepare()
    result = server.call("code_search", {"query": "qqzz_absent_everywhere"})
    assert result["isError"] is False
    assert "no matches" in _text(result)
    assert result["structuredContent"]["matches"] == 0


def test_a_multi_term_query_still_finds_files(server: LiteMcp, monkeypatch: pytest.MonkeyPatch) -> None:
    """As one literal string this would match nothing and cost the agent another call."""
    monkeypatch.setattr(LiteIndex, "search", _no_hits)
    result = server.call("code_search", {"query": f"{NEEDLE} qqzz_absent_term handler"})
    assert "src/app.py" in _text(result)
    assert result["structuredContent"]["matches"] > 0


def test_the_kept_files_hold_the_most_terms_not_the_earliest_paths(repo: Path, tmp_path: Path) -> None:
    """Stopping at the first matches in path order returned AGENTS.md for a question about routing."""
    for number in range(12):
        (repo / f"aaa_{number}.md").write_text("route route route route route\n", encoding="utf-8")
    (repo / "zzz_catalog.py").write_text("# the worktree catalog can route calls\n", encoding="utf-8")
    instance = LiteMcp(environment=_env(repo, tmp_path))
    try:
        blocks, _matches = instance._grep_answer(
            instance.context, "how does the worktree catalog route calls", ["."], 2
        )
        lines = "\n".join(str(block["text"]) for block in blocks).splitlines()
        assert lines[0].startswith("→ zzz_catalog.py")
        assert len(lines) == 2
    finally:
        instance.close()


def test_the_walk_does_not_spend_the_matching_budget(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a very large tree the walk alone used to outlast the budget: every file, no match."""
    monkeypatch.setattr(kit_search, "_REGEX_DEADLINE_SECONDS", 0.05)

    def slow_universe(_root: Path) -> frozenset[Path]:
        time.sleep(0.2)
        return frozenset({repo / "src" / "app.py"})

    payload = kit_search.search_workspace(
        path=".",
        content_regex=NEEDLE,
        repo_root=repo,
        hooks=kit_search.SearchHooks(universe=slow_universe),
    )
    assert kit_search.count_grep_hits(payload) == 1


def test_an_unignored_dependency_tree_stays_out_of_the_walk(repo: Path, server: LiteMcp) -> None:
    vendored = repo / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    (vendored / "index.js").write_text(f"// {NEEDLE}\n", encoding="utf-8")
    universe = local_search._universe(repo)(repo)
    assert repo / "src" / "app.py" in universe
    assert vendored / "index.js" not in universe
    # Naming a file outright still reaches it, as rg does.
    named = server.call("grep", {"regex": NEEDLE, "path": "node_modules/pkg/index.js"})
    assert NEEDLE in _text(named)


@pytest.mark.parametrize("pattern", [None, "*.py"])
def test_a_repository_under_a_skipped_name_is_still_searched(tmp_path: Path, pattern: str | None) -> None:
    """A clone under ~/target or ~/build used to report no matches for everything."""
    root = tmp_path / "target" / "proj"
    (root / "node_modules").mkdir(parents=True)
    (root / "app.py").write_text(f"{NEEDLE} = 1\n", encoding="utf-8")
    (root / "node_modules" / "dep.py").write_text(f"{NEEDLE} = 2\n", encoding="utf-8")
    payload = kit_search.search_workspace(
        path=".",
        content_regex=NEEDLE,
        file_glob_patterns=[pattern] if pattern else None,
        repo_root=root,
    )
    text = kit_search.render_grep_text(payload) or ""
    assert "app.py" in text
    assert "dep.py" not in text


def test_a_search_cut_short_does_not_claim_no_matches(server: LiteMcp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LiteIndex, "search", _no_hits)
    monkeypatch.setattr(kit_search, "_RANK_DEADLINE_SECONDS", -1.0)
    result = server.call("code_search", {"query": NEEDLE})
    assert result["isError"] is False
    assert "[lc:grep_partial]" in _text(result)
    assert "no matches" not in _text(result)


def test_grep_keeps_the_requested_scope(server: LiteMcp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LiteIndex, "search", _no_hits)
    assert "src/app.py" not in _text(server.call("code_search", {"query": NEEDLE, "paths": "docs"}))
    assert "src/app.py" in _text(server.call("code_search", {"query": NEEDLE, "paths": ["docs", "src"]}))


def test_bad_requests_stay_errors(server: LiteMcp, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("disk vanished")

    monkeypatch.setattr(LiteIndex, "search", fail)
    empty = server.call("code_search", {"query": "   "})
    assert empty["isError"] is True
    assert "query must not be empty" in _text(empty)
    outside = server.call("code_search", {"query": NEEDLE, "paths": "../elsewhere"})
    assert outside["isError"] is True
    assert "index_fallback" not in _text(outside)


def test_a_linked_worktree_greps_its_own_files(
    server: LiteMcp, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", str(linked), "-b", "feature")
    (linked / "only_here.py").write_text(f"# {NEEDLE}\n", encoding="utf-8")
    server._catalog.entries(refresh=True)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("worktree index is broken")

    monkeypatch.setattr(LiteIndex, "adopt_prepared", fail)
    result = server.call("code_search", {"query": NEEDLE, "paths": str(linked)})
    assert result["isError"] is False
    text = _text(result)
    assert text.startswith("[worktree ")
    assert "reason=index_failed" in text
    assert "only_here.py" in text


def test_a_folder_without_git_is_counted_as_grep(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "notes.txt").write_text(f"{NEEDLE}\n", encoding="utf-8")
    instance = LiteMcp(environment=_env(plain, tmp_path))
    try:
        result = instance.call("code_search", {"query": f"{NEEDLE} qqzz_absent_term"})
        assert "[no git: literal grep]" in _text(result)
        assert "notes.txt" in _text(result)
        assert _stats(tmp_path)["grep_fallback_reasons"] == {"no_git": 1}
    finally:
        instance.close()


@pytest.mark.parametrize("command", [f"grep -rn {NEEDLE} src", f"cd src && grep -n {NEEDLE} app.py | head -5"])
def test_shell_search_of_indexed_source_is_never_denied(server: LiteMcp, command: str) -> None:
    server.prepare()
    result = server.call("bash", {"command": command})
    assert result["isError"] is False
    assert NEEDLE in _text(result)


def test_the_trace_opt_out_writes_nothing_and_the_answer_is_unchanged(repo: Path, tmp_path: Path) -> None:
    _break_schema(repo)
    instance = LiteMcp(environment={**_env(repo, tmp_path), "LEMONCROW_RUNTIME_TRACE": "off"})
    try:
        result = instance.call("code_search", {"query": NEEDLE})
        assert result["isError"] is False and "src/app.py" in _text(result)
        assert _stats(tmp_path)["total"] == 0
    finally:
        instance.close()


def _write_trace(traces: Path, day: str, tool: str, site: str, **outcome: object) -> None:
    directory = traces / day
    directory.mkdir(parents=True, exist_ok=True)
    record = {"tool": tool, "execution": {"site": site}, "outcome": outcome}
    line = json.dumps(record, separators=(",", ":"))
    with (directory / "client-1.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def test_the_summary_separates_indexed_fallback_and_errors(tmp_path: Path) -> None:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    _write_trace(tmp_path, today, "code_search", "lite", is_error=False, degraded_reason=None)
    _write_trace(tmp_path, today, "code_search", "lite", is_error=False, degraded_reason="index_empty")
    _write_trace(tmp_path, today, "code_search", "lite", is_error=False, degraded_reason="index_empty")
    _write_trace(tmp_path, today, "code_search", "lite", is_error=False, degraded_reason="index_failed")
    _write_trace(tmp_path, today, "code_search", "lite", is_error=True, degraded_reason=None)
    _write_trace(tmp_path, today, "code_search", "server", is_error=False, degraded_reason=None)
    _write_trace(tmp_path, today, "bash", "lite", is_error=False, degraded_reason=None)
    _write_trace(tmp_path, "2000-01-01", "code_search", "lite", is_error=False, degraded_reason=None)
    assert summarize_code_search(tmp_path) == {
        "days": 14,
        "total": 5,
        "indexed": 1,
        "grep_fallback": 3,
        "grep_fallback_reasons": {"index_empty": 2, "index_failed": 1},
        "errors": 1,
        "bash_calls": 1,
        "bash_rg_grep": 0,
        "bash_auto_upgraded": 0,
        "bash_source_search_redirected": 0,
        "bash_rg_grep_preserved": 0,
    }


def test_the_summary_of_nothing_is_zero(tmp_path: Path) -> None:
    assert summarize_code_search(tmp_path / "absent")["total"] == 0


def test_search_stats_command(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _break_schema(repo)
    instance = LiteMcp(environment=_env(repo, tmp_path))
    try:
        instance.call("code_search", {"query": NEEDLE})
    finally:
        instance.close()
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "state"))

    plain = io.StringIO()
    assert cli.main(["runtime", "search-stats"], plain) == 0
    assert "grep fallback: 1" in plain.getvalue()
    assert "index_upgrade_required: 1" in plain.getvalue()
    assert "auto-upgraded to code_search:" in plain.getvalue()
    assert "left as Bash:" in plain.getvalue()

    as_json = io.StringIO()
    assert cli.main(["runtime", "search-stats", "--json", "--days", "3"], as_json) == 0
    assert json.loads(as_json.getvalue())["days"] == 3
    assert cli.main(["runtime", "search-stats", "--days=7"], io.StringIO()) == 0

    for bad in (["--days"], ["--days=0"], ["--days=x"], ["--nope"]):
        assert cli.main(["runtime", "search-stats", *bad], io.StringIO()) == 2
