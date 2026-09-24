from __future__ import annotations

from typing import Any

from lemoncrow.gateway.adapters.mcp.control import handle_control_request
from lemoncrow.gateway.tools.surface import MCP_PROTOCOL_VERSION, SERVER_DISPLAY_NAME, SERVER_VERSION


def _visible(_name: str, _spec: dict[str, Any]) -> bool:
    return True


def _describe(spec: dict[str, Any]) -> str:
    return str(spec.get("description") or "")


def test_initialize_is_handled_with_canonical_server_identity() -> None:
    starts: list[bool] = []
    result = handle_control_request(
        "initialize",
        7,
        on_session_start=lambda: starts.append(True),
        visibility=_visible,
        description=_describe,
    )
    assert starts == [True]
    assert result.handled is True
    assert result.response is not None
    payload = result.response["result"]
    assert payload["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert payload["serverInfo"]["name"] == SERVER_DISPLAY_NAME
    assert payload["serverInfo"]["version"] == SERVER_VERSION


def test_initialized_notification_is_handled_without_response() -> None:
    result = handle_control_request(
        "notifications/initialized",
        None,
        on_session_start=lambda: None,
        visibility=_visible,
        description=_describe,
    )
    assert result.handled is True
    assert result.response is None


def test_unknown_control_method_is_not_claimed() -> None:
    result = handle_control_request(
        "tools/call",
        1,
        on_session_start=lambda: None,
        visibility=_visible,
        description=_describe,
    )
    assert result.handled is False
    assert result.response is None
