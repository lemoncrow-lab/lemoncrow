from __future__ import annotations

from pydantic import BaseModel, ValidationError

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp.errors import tool_error_code, tool_exception_response
from lemoncrow.gateway.adapters.mcp.framework import _ToolArgumentError
from lemoncrow.infra.storage.memory_store import MemoryConcurrencyError, MemorySidecarUnavailable


class _Required(BaseModel):
    value: int


def test_mcp_server_reexports_canonical_error_code_helper() -> None:
    assert mcp_server._tool_error_code is tool_error_code


def test_argument_error_keeps_invalid_params_code() -> None:
    response = tool_exception_response(7, _ToolArgumentError("bad arg"))
    assert response["error"] == {"code": -32602, "message": "bad arg"}


def test_validation_error_keeps_invalid_params_code() -> None:
    try:
        _Required.model_validate({})
    except ValidationError as exc:
        response = tool_exception_response(7, exc)
    else:  # pragma: no cover
        raise AssertionError("expected validation error")
    assert response["error"]["code"] == -32602


def test_memory_errors_keep_deliberate_protocol_codes() -> None:
    assert tool_exception_response(1, MemoryConcurrencyError("busy"))["error"]["code"] == 409
    assert tool_exception_response(1, MemorySidecarUnavailable("down"))["error"]["code"] == 503


def test_execution_failure_is_mcp_tool_error_not_jsonrpc_error() -> None:
    response = tool_exception_response(9, RuntimeError("boom"))
    assert "error" not in response
    assert response["result"] == {
        "content": [{"type": "text", "text": "boom"}],
        "isError": True,
    }
