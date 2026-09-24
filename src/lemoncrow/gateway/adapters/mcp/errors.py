"""MCP framing for transport-neutral tool failure classification."""

from __future__ import annotations

from typing import Any

from lemoncrow.gateway.adapters.mcp.jsonrpc import error, ok
from lemoncrow.gateway.tools.errors import classify_tool_exception, execution_error_payload


def tool_error_code(exc: Exception) -> int:
    """Compatibility helper returning the classified protocol code or -32000."""
    disposition = classify_tool_exception(exc)
    return disposition.protocol_code if disposition.protocol_code is not None else -32000


def tool_exception_response(request_id: Any, exc: Exception) -> dict[str, Any]:
    """Map a post-dispatch exception to the MCP-compliant response shape."""
    disposition = classify_tool_exception(exc)
    if disposition.protocol_code is not None:
        return error(request_id, disposition.protocol_code, disposition.message)
    return ok(request_id, execution_error_payload(exc))


__all__ = ["tool_error_code", "tool_exception_response"]
