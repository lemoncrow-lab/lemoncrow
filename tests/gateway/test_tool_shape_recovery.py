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
    mcp_server._current_ledger = None
    mcp_server._realtime_ctx = None
    mcp_server._remote_client = MagicMock()
    mcp_server._remote_client.get_context.return_value = {"context": "", "run_ledger": []}
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


def test_alias_registry_covers_vanilla_habits() -> None:
    tools = mcp_server.TOOLS
    assert tools["read"]["param_aliases"]["file_path"] == "path"
    assert tools["grep"]["param_aliases"]["pattern"] == "regex"
    assert tools["grep"]["param_aliases"]["-i"] == "i"
    assert tools["code_search"]["param_aliases"]["pattern"] == "query"
    assert tools["code_search"]["param_aliases"]["max_results"] == "max_files"
    assert tools["web_fetch"]["param_aliases"]["format"] == "type"


def test_bash_schema_advertises_cwd() -> None:
    props = mcp_server.BASH_TOOL_INPUT_SCHEMA["properties"]
    assert "cwd" in props
    assert "persist" in props["cwd"]["description"]
    assert "cwd" in mcp_server.TOOLS["bash"]["description"]
