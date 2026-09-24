from __future__ import annotations

from lemoncrow.gateway.adapters.mcp.framework import _ToolArgumentError
from lemoncrow.gateway.tools.errors import ToolArgumentError, classify_tool_exception, execution_error_payload


def test_framework_reexports_canonical_tool_argument_error() -> None:
    assert _ToolArgumentError is ToolArgumentError


def test_canonical_tool_argument_error_is_a_value_error() -> None:
    err = ToolArgumentError("bad")
    assert isinstance(err, ValueError)
    assert str(err) == "bad"


def test_exception_classification_is_transport_neutral() -> None:
    arg = classify_tool_exception(ToolArgumentError("bad"))
    assert arg.protocol_code == -32602
    assert arg.is_execution_error is False

    execution = classify_tool_exception(RuntimeError("boom"))
    assert execution.protocol_code is None
    assert execution.is_execution_error is True
    assert execution_error_payload(RuntimeError("boom")) == {
        "content": [{"type": "text", "text": "boom"}],
        "isError": True,
    }
