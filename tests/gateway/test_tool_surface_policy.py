from __future__ import annotations

from lemoncrow.core.environment import CORE_MCP_TOOLS, LLM_VISIBLE_TOOLS, mcp_tool_visible_to_llm
from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import surface


def test_mcp_server_reexports_canonical_surface_identity() -> None:
    assert mcp_server.PROTOCOL_VERSION == surface.MCP_PROTOCOL_VERSION
    assert mcp_server.SERVER_NAME == surface.SERVER_NAME
    assert mcp_server.SERVER_DISPLAY_NAME == surface.SERVER_DISPLAY_NAME
    assert mcp_server.SERVER_VERSION == surface.SERVER_VERSION
    assert mcp_server.SERVER_INSTRUCTIONS == surface.SERVER_INSTRUCTIONS
    assert mcp_server._CORE_MCP_TOOLS == surface.CORE_MCP_TOOLS


def test_mcp_server_profile_wrapper_matches_canonical_policy(monkeypatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_TOOL_PROFILE", "core")
    assert surface.mcp_tool_profile() == "core"
    for name in ("bash", "code_search", "tool", "node"):
        assert mcp_server._tool_profile_exposes(name) == surface.tool_profile_exposes(name)


def test_surface_policy_is_transport_independent() -> None:
    source = surface.__file__
    assert source is not None
    text = open(source, encoding="utf-8").read()
    assert "mcp_server" not in text


def test_llm_surface_is_explicit_allowlist_and_fails_closed() -> None:
    assert CORE_MCP_TOOLS <= LLM_VISIBLE_TOOLS
    assert LLM_VISIBLE_TOOLS == {"bash", "code_search", "edit", "read", "web_fetch"}
    assert mcp_tool_visible_to_llm("newly_registered_tool_not_allowlisted") is False
    for name in LLM_VISIBLE_TOOLS:
        assert mcp_tool_visible_to_llm(name) is True
