"""Integration tests: the spiral nudge is surfaced through the MCP _handle path.

Repeating a byte-identical tool call cannot change the result; once it crosses
the threshold the dispatch appends a one-line ``[loop]`` note to the rendered
text so an external host (Claude Code, ...) gets the same no-progress signal
LemonCrow's own runtime already acts on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.gateway.adapters import mcp_server


def _call_bash(command: str, rid: int) -> str:
    resp = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": "bash", "arguments": {"command": command}},
        }
    )
    assert resp is not None
    return resp["result"]["content"][0]["text"]


def test_repeated_identical_call_surfaces_nudge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_CONTEXT_DEDUP", "0")  # keep full text, not a dedup stub
    mcp_server._loop_tracker_sessions.clear()

    texts = [_call_bash("echo spiral", rid) for rid in range(1, 6)]
    assert "[loop]" not in texts[0]  # first call: clean
    assert "[loop]" not in texts[2]  # third: still below threshold (4)
    assert "[loop]" in texts[3]  # fourth identical call trips the nudge
    assert "echo spiral" in texts[3] or "spiral" in texts[3]


def test_off_switch_disables_nudge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_CONTEXT_DEDUP", "0")
    monkeypatch.setenv("LEMONCROW_LOOP_REVIEW", "0")
    mcp_server._loop_tracker_sessions.clear()

    texts = [_call_bash("echo quiet", rid) for rid in range(1, 7)]
    assert all("[loop]" not in t for t in texts)


def test_distinct_calls_do_not_nudge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_CONTEXT_DEDUP", "0")
    mcp_server._loop_tracker_sessions.clear()

    texts = [_call_bash(f"echo run{i}", rid) for rid, i in enumerate(range(6), start=1)]
    assert all("[loop]" not in t for t in texts)
