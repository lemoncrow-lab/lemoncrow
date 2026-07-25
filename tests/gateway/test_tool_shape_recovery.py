"""Call-shape recovery + advertised-surface tests for read/grep/bash/code_search/web_fetch.

Companion to the edit-tool flattened-call recovery: vanilla-host habit arg names
(the built-in Read/Grep tools' literal params) must work, the bash schema must
advertise cwd, and the read paging escalation must never destroy a batched
range read or spill a file that exceeds the inline budget.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from lemoncrow.gateway.adapters import mcp_server
from tests.helpers import init_store_at


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    req: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    resp = mcp_server._handle(req)
    assert isinstance(resp, dict)
    return resp


def _text(resp: dict[str, Any]) -> str:
    assert "result" in resp, resp
    return str(resp["result"]["content"][0]["text"])


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".lemoncrow"
    init_store_at(str(root))
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    mcp_server._ledger._current_ledger = None
    mcp_server._ledger._realtime_ctx = None
    mcp_server._remote_client = MagicMock()
    mcp_server._remote_client.get_context.return_value = {"context": "", "run_ledger": []}
    # The repeat-query bucket is process-wide ("_global" stdio bucket) --
    # without clearing it, an earlier test's code_search query (e.g. the
    # common "src.py" fixture file) can blank out a later, unrelated test.
    mcp_server._RECENT_CODE_SEARCH_QUERIES.clear()
    # Same rationale, for the bash-cat/sed redundancy backstop's own
    # process-wide "_global" bucket (lives in mcp.ledger, not mcp_server).
    mcp_server._ledger._RECENT_FULL_READS.clear()
    return tmp_path


# ---------------------------------------------------------------------------
# read: vanilla Read-tool habits (file_path / offset / limit)
# ---------------------------------------------------------------------------


def test_read_file_path_offset_limit(workspace: Path) -> None:
    """read(file_path=..., offset=10, limit=5) -- the built-in Read tool's exact
    arg names -- returns lines 10-14 instead of an unknown-argument error."""
    f = workspace / "big.py"
    f.write_text("\n".join(f"line_{i:04d} = {i}" for i in range(1, 301)) + "\n", encoding="utf-8")

    text = _text(_call("read", {"file_path": "big.py", "offset": 10, "limit": 5}))

    assert "line_0010" in text
    assert "line_0014" in text
    assert "line_0015" not in text
    assert "line_0009" not in text


def test_read_files_entry_accepts_file_path_key(workspace: Path) -> None:
    f = workspace / "entry.py"
    f.write_text("MARKER_ENTRY = 1\n", encoding="utf-8")

    text = _text(_call("read", {"files": [{"file_path": "entry.py"}]}))

    assert "MARKER_ENTRY" in text


# ---------------------------------------------------------------------------
# read: ranged reads are served exactly as requested -- never silently widened
# ---------------------------------------------------------------------------


def test_read_batched_ranges_serve_exact_slices(workspace: Path) -> None:
    """Several ranges of one file in a SINGLE files=[] call return exactly those
    slices -- nothing outside the requested lines."""
    f = workspace / "wide.py"
    f.write_text("\n".join(f"row_{i:04d} = {i}" for i in range(1, 1001)) + "\n", encoding="utf-8")

    text = _text(_call("read", {"files": ["wide.py:L1-L5", "wide.py:L100-L104", "wide.py:L900-L904"]}))

    assert "row_0001" in text and "row_0100" in text and "row_0900" in text
    assert "row_0006" not in text, "slice widened beyond the requested lines"
    assert "row_0500" not in text, "batched ranges widened to a whole-file read"
    assert "row_0999" not in text, "batched ranges widened to a whole-file read"


def test_read_repeated_ranges_never_widen_to_whole_file(workspace: Path) -> None:
    """Repeated ranged reads of the SAME file across separate calls keep
    returning exactly the requested slice. (A former 3-call escalation
    heuristic dumped the whole file here; it misfired on scattered
    spot-checks -- e.g. a 40-line request answered with a 900-line file --
    and was removed: precise slice > vague complete source.)"""
    f = workspace / "paged.py"
    f.write_text("\n".join(f"p_{i:04d} = {i}" for i in range(1, 1001)) + "\n", encoding="utf-8")

    for start in (1, 50, 100, 150):  # 4 separate calls: would have crossed the old 3-call threshold
        text = _text(_call("read", {"files": [f"paged.py:L{start}-L{start + 4}"]}))
        assert f"p_{start:04d}" in text
        assert f"p_{start + 4:04d}" in text
        assert f"p_{start + 5:04d}" not in text, "ranged read widened beyond the requested slice"
        assert "p_0999" not in text, "ranged read escalated to the whole file"


# ---------------------------------------------------------------------------
# grep / code_search / web_fetch / bash advertised surface
# ---------------------------------------------------------------------------


def test_grep_pattern_alias_end_to_end(workspace: Path) -> None:
    (workspace / "src.py").write_text("NEEDLE_TOKEN = 1\n", encoding="utf-8")

    text = _text(_call("grep", {"pattern": "NEEDLE_TOKEN", "path": "."}))

    assert "NEEDLE_TOKEN" in text


def test_code_search_limit_param_end_to_end(workspace: Path) -> None:
    """code_search(..., limit=N) -- the common search-API kwarg name -- is the
    real parameter now (candidate_files cap), not an alias to anything."""
    (workspace / "src.py").write_text("NEEDLE_TOKEN = 1\n", encoding="utf-8")

    # Fast-path on a literal existing file path -- pinned directly, no
    # background-index timing dependency (mirrors
    # test_tool_code_search_fast_paths_a_literal_file_path).
    text = _text(_call("code_search", {"query": "src.py", "limit": 3}))

    assert "unknown argument" not in text
    assert "NEEDLE_TOKEN" in text


@pytest.mark.parametrize("vanilla_name", ["max_files", "maxFiles", "max_results", "max_candidates"])
def test_code_search_limit_vanilla_aliases_end_to_end(workspace: Path, vanilla_name: str) -> None:
    """Every legacy/vanilla-habit spelling for "how many results" resolves to
    the single real `limit` param instead of an unknown-argument MCP error --
    there is exactly one agent-visible count knob on code_search now."""
    (workspace / "src.py").write_text("NEEDLE_TOKEN = 1\n", encoding="utf-8")

    text = _text(_call("code_search", {"query": "src.py", vanilla_name: 2}))

    assert "unknown argument" not in text
    assert "NEEDLE_TOKEN" in text


def test_code_search_blanks_near_duplicate_query_end_to_end(workspace: Path) -> None:
    """Two identical code_search calls in a row -- the exact regression shape
    from a real debt-benchmark rep (17+ near-duplicate calls, 2026-07-25) --
    the SECOND call must return blank (no engine call, no explanatory text --
    a prose hint was tried and measured to be skimmed past and ignored).
    Fast-pathed query (a literal existing file path) so the first call's
    content is deterministic, not dependent on background-index timing.
    """
    mcp_server._RECENT_CODE_SEARCH_QUERIES.clear()
    (workspace / "src.py").write_text("NEEDLE_TOKEN = 1\n", encoding="utf-8")

    first = _text(_call("code_search", {"query": "src.py"}))
    second = _text(_call("code_search", {"query": "src.py"}))

    assert "NEEDLE_TOKEN" in first
    assert second == "no exact match -- ranked candidates"
    assert "[lc:" not in second


def test_alias_registry_covers_vanilla_habits() -> None:
    tools = mcp_server.TOOLS
    assert tools["read"]["param_aliases"]["file_path"] == "path"
    assert tools["grep"]["param_aliases"]["pattern"] == "regex"
    assert tools["grep"]["param_aliases"]["-i"] == "i"
    assert tools["code_search"]["param_aliases"]["pattern"] == "query"
    # code_search has exactly one agent-visible count knob (`limit`, a real
    # param, not aliased) -- every vanilla/legacy spelling for it redirects
    # here instead of landing on a separate, confusing second parameter.
    assert tools["code_search"]["param_aliases"]["max_results"] == "limit"
    assert tools["code_search"]["param_aliases"]["max_files"] == "limit"
    assert tools["code_search"]["param_aliases"]["maxFiles"] == "limit"
    assert tools["code_search"]["param_aliases"]["max_candidates"] == "limit"
    assert "limit" not in tools["code_search"]["param_aliases"]  # real param, not an alias
    assert tools["web_fetch"]["param_aliases"]["format"] == "type"


def test_bash_schema_advertises_cwd() -> None:
    props = mcp_server.BASH_TOOL_INPUT_SCHEMA["properties"]
    assert "cwd" in props
    assert "persist" in props["cwd"]["description"]
    assert "cwd" in mcp_server.TOOLS["bash"]["description"]


# ---------------------------------------------------------------------------
# bash: redundant re-dump of a path already `read` in full this session
# ---------------------------------------------------------------------------


def test_bash_cat_blocked_after_full_read_end_to_end(workspace: Path) -> None:
    (workspace / "debt.py").write_text("x = 1\n" * 5, encoding="utf-8")

    read_text = _text(_call("read", {"files": ["debt.py:full"]}))
    assert "x = 1" in read_text

    cat_text = _text(_call("bash", {"command": "cat debt.py"}))
    assert cat_text == "[lc: already read in full this session -- reuse that content, don't re-dump]"


def test_bash_cat_not_blocked_for_a_file_never_read(workspace: Path) -> None:
    (workspace / "other.py").write_text("y = 2\n", encoding="utf-8")

    cat_text = _text(_call("bash", {"command": "cat other.py"}))
    assert "y = 2" in cat_text


def test_bash_cat_not_blocked_when_piped(workspace: Path) -> None:
    (workspace / "debt.py").write_text("x = 1\n" * 5, encoding="utf-8")
    _call("read", {"files": ["debt.py:full"]})

    cat_text = _text(_call("bash", {"command": "cat debt.py | wc -l"}))
    assert cat_text != "[lc: already read in full this session -- reuse that content, don't re-dump]"
    assert "5" in cat_text


# ---------------------------------------------------------------------------
# read/code_search: the dedup stub's own suggested recovery (force=true) must
# actually be a reachable argument, not an unknown-argument error -- a repeat
# debt-benchmark rep hit exactly this: 3 dead `read` retries because
# force=true was rejected, before the model gave up and brute-forced the edit
# via a bash heredoc rewrite instead.
# ---------------------------------------------------------------------------


def test_read_force_true_is_not_an_unknown_argument(workspace: Path) -> None:
    big = "VALUE = 1\n" * 1000  # comfortably over the dedup's 4096-char floor
    (workspace / "big.py").write_text(big, encoding="utf-8")

    first = _text(_call("read", {"files": ["big.py:full"]}))
    second = _text(_call("read", {"files": ["big.py:full"]}))
    forced = _text(_call("read", {"files": ["big.py:full"], "force": True}))

    assert "VALUE = 1" in first
    assert "[dedup]" in second and "force=true" in second
    assert "error" not in forced.lower()
    assert "VALUE = 1" in forced


def test_code_search_force_true_is_not_an_unknown_argument(workspace: Path) -> None:
    (workspace / "needle.py").write_text("NEEDLE_TOKEN = 1\n", encoding="utf-8")

    _call("code_search", {"query": "needle.py"})
    forced = _text(_call("code_search", {"query": "needle.py", "force": True}))

    assert "error" not in forced.lower()


def test_read_force_not_in_advertised_schema(workspace: Path) -> None:
    assert "force" not in mcp_server.TOOLS["read"]["inputSchema"]["properties"]
    assert "force" not in mcp_server.TOOLS["code_search"]["inputSchema"]["properties"]


# ---------------------------------------------------------------------------
# Host-metadata keys: Cursor's native tool convention trains models to attach
# a description/explanation label to every call. Rejecting those threw away
# the whole emitted call (observed twice per debt-benchmark rep on `bash`).
# They must be silently dropped for every tool that has no such parameter.
# ---------------------------------------------------------------------------


def test_bash_description_metadata_key_is_dropped(workspace: Path) -> None:
    out = _text(_call("bash", {"command": "echo hi", "description": "Say hi"}))
    assert "unknown argument" not in out
    assert "hi" in out


def test_read_explanation_metadata_key_is_dropped(workspace: Path) -> None:
    (workspace / "meta.py").write_text("META = 1\n", encoding="utf-8")
    out = _text(_call("read", {"files": ["meta.py"], "explanation": "look at meta"}))
    assert "unknown argument" not in out
    assert "META = 1" in out


def test_bash_is_background_native_alias(workspace: Path) -> None:
    out = _text(_call("bash", {"command": "echo bgtest", "is_background": False}))
    assert "unknown argument" not in out
    assert "bgtest" in out


def test_bash_millisecond_timeout_treated_as_ms(workspace: Path) -> None:
    # Cursor-style ms value (120000 meaning 120s) must not become a 33h wait
    # budget -- and must not error.
    out = _text(_call("bash", {"command": "echo fast", "timeout": 120000}))
    assert "unknown argument" not in out
    assert "fast" in out
