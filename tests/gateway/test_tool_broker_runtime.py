from __future__ import annotations

from typing import Any

import pytest

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp.framework import _ToolArgumentError
from lemoncrow.gateway.tools import broker, state


def test_mcp_server_reexports_canonical_tool_call_state() -> None:
    assert mcp_server._tool_call_counterfactual is state.tool_call_counterfactual
    assert mcp_server._tool_call_rendered_text is state.tool_call_rendered_text
    assert mcp_server._tool_call_raw_result is state.tool_call_raw_result
    assert mcp_server._tool_call_images is state.tool_call_images


def test_broker_search_is_transport_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    tools: dict[str, dict[str, Any]] = {
        "read": {"name": "read", "description": "core read", "handler": lambda _args: {}},
        "rare": {"name": "rare", "description": "Inspect unusual dependency state", "handler": lambda _args: {}},
        "other": {"name": "other", "description": "Unrelated capability", "handler": lambda _args: {}},
    }
    monkeypatch.setattr(broker, "registered_tools", lambda: tools)
    state.tool_call_rendered_text.value = None

    result = broker.invoke_tool_broker(
        {"action": "search", "query": "dependency"},
        renderer=lambda _name, _result: None,
        visibility=lambda _name, _spec: True,
    )

    assert result == {"matches": [{"name": "rare", "description": "Inspect unusual dependency state"}]}
    assert state.tool_call_rendered_text.value == "tools\n→ rare · Inspect unusual dependency state"


def test_broker_call_invokes_exact_registered_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        seen.update(args)
        return {"ok": True, "value": args["value"]}

    monkeypatch.setattr(
        broker,
        "registered_tools",
        lambda: {"rare": {"name": "rare", "description": "Rare tool", "handler": handler}},
    )
    state.tool_call_rendered_text.value = None

    result = broker.invoke_tool_broker(
        {"action": "call", "name": "rare", "arguments": {"value": 7}},
        renderer=lambda name, payload: f"{name}:{payload['value']}",
        visibility=lambda _name, _spec: True,
    )

    assert result == {"ok": True, "value": 7}
    assert seen == {"value": 7}
    assert state.tool_call_rendered_text.value == "rare:7"


def test_broker_refuses_direct_core_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        broker,
        "registered_tools",
        lambda: {"read": {"name": "read", "description": "Read", "handler": lambda _args: {}}},
    )
    with pytest.raises(_ToolArgumentError, match="already exposed"):
        broker.invoke_tool_broker(
            {"action": "call", "name": "read", "arguments": {}},
            renderer=lambda _name, _result: None,
            visibility=lambda _name, _spec: True,
        )
