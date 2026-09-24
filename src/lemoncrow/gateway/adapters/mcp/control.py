"""Protocol-level MCP control requests that do not execute tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lemoncrow.gateway.adapters.mcp.jsonrpc import ok
from lemoncrow.gateway.adapters.mcp_branding import icon_metadata
from lemoncrow.gateway.tools.registry import advertised_tools
from lemoncrow.gateway.tools.surface import (
    MCP_PROTOCOL_VERSION,
    SERVER_DISPLAY_NAME,
    SERVER_INSTRUCTIONS,
    SERVER_VERSION,
)


@dataclass(frozen=True, slots=True)
class ControlRequestResult:
    """Outcome of trying to handle a non-tool MCP control request."""

    handled: bool
    response: dict[str, Any] | None


def handle_control_request(
    method: Any,
    request_id: Any,
    *,
    on_session_start: Callable[[], None],
    visibility: Callable[[str, dict[str, Any]], bool],
    description: Callable[[dict[str, Any]], str],
) -> ControlRequestResult:
    """Handle initialize/initialized/tools-list without entering tool execution."""
    if method == "initialize":
        on_session_start()
        return ControlRequestResult(
            handled=True,
            response=ok(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "serverInfo": {
                        "name": SERVER_DISPLAY_NAME,
                        "title": "LemonCrow",
                        "version": SERVER_VERSION,
                        "description": (
                            "Indexed code search, bounded file tools, editing, and verification for coding agents."
                        ),
                        "icons": [icon_metadata()],
                    },
                    "capabilities": {"tools": {}},
                    "instructions": SERVER_INSTRUCTIONS,
                },
            ),
        )
    if method == "notifications/initialized":
        return ControlRequestResult(handled=True, response=None)
    if method == "tools/list":
        return ControlRequestResult(
            handled=True,
            response=ok(
                request_id,
                {
                    "tools": advertised_tools(
                        visibility=visibility,
                        description=description,
                    )
                },
            ),
        )
    return ControlRequestResult(handled=False, response=None)


__all__ = ["ControlRequestResult", "handle_control_request"]
