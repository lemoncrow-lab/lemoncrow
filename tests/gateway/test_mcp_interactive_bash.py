"""MCP-surface tests for interactive bash sessions: the `interactive`,
`input`, and `idle_ttl` parameters and action="send" wiring through
_run_bash_tool / tool_bash."""

from __future__ import annotations

import pytest

import lemoncrow.gateway.adapters.mcp_server as mcp_server


def _cancel(session_id: str) -> None:
    try:
        mcp_server._run_bash_tool(session_id=session_id, action="kill")
    except KeyError:
        pass


def test_interactive_run_returns_a_handle_immediately() -> None:
    result = mcp_server._run_bash_tool("python3 -u -i -q", interactive=True, idle_ttl=30)
    assert isinstance(result, dict)
    sid = str(result["session_id"])
    try:
        assert result["status"] == "running"
        assert result["interactive"] is True

        sent = mcp_server._run_bash_tool(session_id=sid, action="send", input_text="print(6 * 7)", timeout=10)
        assert isinstance(sent, dict)
        assert "42" in str(sent["stdout"])
    finally:
        _cancel(sid)


def test_send_requires_a_session_id() -> None:
    with pytest.raises(ValueError, match="session_id is required"):
        mcp_server._run_bash_tool(action="send", input_text="print(1)")


def test_interactive_cannot_be_background() -> None:
    with pytest.raises(ValueError, match="bg=true"):
        mcp_server._run_bash_tool("python3 -u -i -q", interactive=True, background=True)


def test_tool_bash_input_implies_send() -> None:
    started = mcp_server._run_bash_tool("python3 -u -i -q", interactive=True, idle_ttl=30)
    assert isinstance(started, dict)
    sid = str(started["session_id"])
    try:
        # tool_bash is the registered MCP handler: one dict of raw args.
        rendered = mcp_server.tool_bash({"id": sid, "input": "print('via-tool-bash')", "timeout": 10})
        assert isinstance(rendered, str)
        assert "via-tool-bash" in rendered
    finally:
        _cancel(sid)


def test_schema_advertises_interactive_params() -> None:
    props = mcp_server.BASH_TOOL_INPUT_SCHEMA["properties"]
    assert "interactive" in props
    assert "input" in props
    # idle_ttl is intentionally hidden from the wire schema (still accepted by
    # tool_bash/_run_bash_tool -- see test_interactive_run_returns_a_handle_immediately)
    # so the LLM isn't shown a knob it rarely needs to set.
    assert "idle_ttl" not in props
    assert "send" in props["action"]["enum"]
