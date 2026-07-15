"""Host-mode cache-hygiene + result-bounding tests for the MCP dispatch path.

When LemonCrow is a guest MCP server inside a host agent (Claude Code / Codex), the
host re-sends the whole conversation each turn, so LemonCrow's tool schemas and
tool results ride in the host's cached prompt. These cover the two levers LemonCrow
actually controls there: deterministic ordering (so the prefix cache stays warm)
and tail-preserving compaction of runaway results.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lemoncrow.gateway.adapters import mcp_server


def test_compact_result_text_passes_small_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", raising=False)
    small = "hello world"
    assert mcp_server._compact_result_text(small, "read") == small


def test_compact_result_text_compacts_oversized_keeping_head_and_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", "1000")
    text = "HEAD" + ("m" * 20000) + "TAIL"
    out = mcp_server._compact_result_text(text, "bash")
    assert len(out) < len(text)
    assert out.startswith("HEAD")  # head preserved
    assert "TAIL" in out  # tail preserved -- the win over head-only truncation
    assert "omitted" in out  # omission marker from compress_tool_output
    assert "[lc: compacted" in out  # recovery footer


def test_compact_result_text_disabled_with_env_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", "0")
    text = "z" * 50000
    assert mcp_server._compact_result_text(text, "read") == text


def test_compact_result_text_invalid_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", "not-an-int")
    # default is 256 KiB; a sub-threshold payload passes through unchanged
    text = "a" * 1000
    assert mcp_server._compact_result_text(text, "grep") == text


def test_compact_result_text_spills_full_text_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bare "narrow the query" hint used to leave the dropped middle
    unrecoverable. With T7 spill enabled (default), the full pre-compaction
    text is persisted and the hint names a recoverable path instead.
    """
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path / "spill"))
    monkeypatch.delenv("LEMONCROW_TOOL_OUTPUT_SPILL", raising=False)  # default on
    monkeypatch.setenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", "1000")
    middle_marker = "UNIQUE-MIDDLE-MARKER"
    text = "HEAD" + ("m" * 10000) + middle_marker + ("m" * 10000) + "TAIL"
    out = mcp_server._compact_result_text(text, "bash")

    assert middle_marker not in out  # the summary still drops the middle
    assert "[lc: compacted" in out
    match = re.search(r"full: (\S+\.txt)\]", out)
    assert match is not None
    recovered = Path(match.group(1)).read_text(encoding="utf-8")
    assert recovered == text
    assert middle_marker in recovered  # fully recoverable from the spill file


def test_compact_result_text_falls_back_to_narrow_hint_when_spill_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "0")
    monkeypatch.setenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", "1000")
    text = "HEAD" + ("m" * 20000) + "TAIL"
    out = mcp_server._compact_result_text(text, "bash")
    assert "spilled to" not in out
    assert "narrow the query for full" in out


def test_tools_list_is_sorted_by_name() -> None:
    resp = mcp_server._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert resp is not None
    names = [tool["name"] for tool in resp["result"]["tools"]]
    assert names == sorted(names)
