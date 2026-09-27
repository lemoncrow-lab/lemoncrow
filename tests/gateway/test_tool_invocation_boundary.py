from __future__ import annotations

import json

import pytest

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools.invocation import REMOTE_TOOLS, ToolCallPreparationError, prepare_tool_call


def _prepare(params: dict[str, object]):
    return prepare_tool_call(
        params,
        broker_spec=mcp_server._TOOL_BROKER_SPEC,
        remote_tools=mcp_server._REMOTE_TOOLS,
    )


def test_run_alias_and_json_string_arguments_are_normalized() -> None:
    prepared = _prepare({"name": "run", "arguments": json.dumps({"command": "printf ok"})})
    assert prepared.name == "bash"
    assert prepared.args == {"command": "printf ok"}
    assert prepared.spec is mcp_server.TOOLS["bash"]


def test_non_object_arguments_fall_back_to_empty_object() -> None:
    prepared = _prepare({"name": "read", "arguments": ["not", "an", "object"]})
    assert prepared.args == {}


def test_unknown_tool_is_protocol_error() -> None:
    with pytest.raises(ToolCallPreparationError) as excinfo:
        _prepare({"name": "definitely-not-a-tool", "arguments": {}})
    assert excinfo.value.code == -32601
    assert str(excinfo.value) == "unknown tool: definitely-not-a-tool"


def test_memory_rejects_unknown_arguments_before_handler() -> None:
    with pytest.raises(ToolCallPreparationError) as excinfo:
        _prepare({"name": "memory", "arguments": {"op": "recall", "definitely_unknown": True}})
    assert excinfo.value.code == -32602
    assert "definitely_unknown" in str(excinfo.value)


def test_context_symbols_bypasses_remote_routing() -> None:
    local = _prepare({"name": "context", "arguments": {"mode": "symbols"}})
    normal = _prepare({"name": "context", "arguments": {}})
    assert local.remote_routed is False
    assert normal.remote_routed is True


def test_mcp_remote_tool_set_reexports_canonical_policy() -> None:
    assert mcp_server._REMOTE_TOOLS is REMOTE_TOOLS
