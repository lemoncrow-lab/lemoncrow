"""Unit tests for the Atelier `agent` MCP tool (owned sub-agent spawn)."""

from __future__ import annotations

from types import SimpleNamespace

from atelier.core.capabilities.owned_execution_routing import NoFeasibleRouteError
from atelier.core.environment import HIDDEN_LLM_TOOLS
from atelier.gateway.adapters import mcp_server


def test_agent_tool_registered_but_hidden() -> None:
    assert "agent" in mcp_server.TOOLS
    assert "agent" in HIDDEN_LLM_TOOLS
    assert not mcp_server._tool_visible_to_llm("agent", mcp_server.TOOLS["agent"])


def test_agent_tool_no_route_is_graceful(monkeypatch) -> None:
    def _raise(*_a, **_k):
        raise NoFeasibleRouteError("no provider")

    monkeypatch.setattr(mcp_server, "select_owned_route", _raise)
    result = mcp_server.tool_agent({"prompt": "do the thing"})
    assert result["isError"] is True
    assert result["status"] == "no_route"
    assert "no provider" in result["message"]


def test_agent_tool_success_shapes_receipt(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "select_owned_route", lambda *_a, **_k: SimpleNamespace())
    receipt = SimpleNamespace(
        status="ok",
        executed_provider="anthropic",
        executed_model="claude-x",
        executed_transport="cli",
        cost_usd=0.01,
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=3,
        cache_write_input_tokens=2,
        cache_evidence="observed",
        reuse_observed=True,
        cache_scope_id="scope-1",
    )
    monkeypatch.setattr(
        mcp_server,
        "execute_owned_prompt",
        lambda *_a, **_k: SimpleNamespace(output="hi there", receipt=receipt),
    )
    result = mcp_server.tool_agent({"prompt": "say hi", "budget": "cheap"})
    assert result["status"] == "ok"
    assert result["output"] == "hi there"
    assert result["provider"] == "anthropic"
    assert result["tokens"]["cache_read"] == 3
    assert result["cache"]["reuse_observed"] is True
