from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp.jsonrpc import error, ok
from lemoncrow.gateway.tools.results import clean_tool_result


def test_mcp_server_reexports_jsonrpc_helpers() -> None:
    assert mcp_server._ok is ok
    assert mcp_server._err is error


def test_mcp_server_reexports_result_normalization() -> None:
    assert mcp_server._clean_tool_result is clean_tool_result


def test_result_normalization_preserves_semantic_empty_values() -> None:
    result = clean_tool_result(
        {
            "none": None,
            "empty": "",
            "zero": 0,
            "false": False,
            "list": [],
            "dict": {},
            "nested": {"drop": "", "keep": "value"},
        },
        "read",
    )
    assert result == {
        "zero": 0,
        "false": False,
        "list": [],
        "dict": {},
        "nested": {"keep": "value"},
    }


def test_jsonrpc_envelopes_are_canonical() -> None:
    assert ok(7, {"value": 1}) == {"jsonrpc": "2.0", "id": 7, "result": {"value": 1}}
    assert error(7, -32601, "missing") == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32601, "message": "missing"},
    }
