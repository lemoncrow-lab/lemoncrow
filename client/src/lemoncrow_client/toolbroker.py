"""The ``tool`` broker for canonically visible tools omitted by a profile.

Mirrors the public server broker in ``lemoncrow.gateway.tools.broker`` while
keeping the dependency-free thin client self-contained. Baseline-hidden,
admin, and internal tools are never discoverable here. Under
``LEMONCROW_MCP_TOOL_PROFILE=core`` the broker can discover only canonically
visible routed tools omitted by that profile. Calls still flow through the
normal :class:`~lemoncrow_client.dispatcher.Dispatcher`; this module adds
discovery, not a parallel execution path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from .dispatcher import Dispatcher, ToolOutcome
from .errors import AgentAction, ClientError, ErrorCode, error_block
from .routing import ROUTES
from .surface import _mcp_tool_profile, load_surface, tool_list

__all__ = ["BROKER_TOOL_NAME", "broker_enabled", "broker_spec", "handle"]

BROKER_TOOL_NAME: Final[str] = "tool"

_DESCRIPTION: Final[str] = (
    "Search or call a routed LemonCrow tool that LEMONCROW_MCP_TOOL_PROFILE=core "
    "keeps off tools/list to save schema tokens. A tool already listed in "
    "tools/list must be called directly instead -- this exists only for the "
    "rest.\n"
    "- action='search': query (substring, optional) -> matching hidden tools.\n"
    "- action='call': name (exact) + arguments -> that tool's normal result."
)

_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["search", "call"]},
        "query": {"type": "string", "description": "action=search: substring matched against name and description."},
        "name": {"type": "string", "description": "action=call: the exact hidden tool name."},
        "arguments": {"type": "object", "description": "action=call: arguments for the target tool."},
    },
    "required": ["action"],
    "additionalProperties": False,
}


def broker_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether ``tools/list`` and ``tools/call`` should recognize the broker."""
    return _mcp_tool_profile(env) == "core"


def broker_spec() -> dict[str, Any]:
    """The ``tools/list`` entry for the broker itself."""
    return {"name": BROKER_TOOL_NAME, "description": _DESCRIPTION, "inputSchema": dict(_INPUT_SCHEMA)}


def _hidden_names(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """Canonically visible routed tools hidden only by the active profile.

    Baseline-hidden/admin/internal tools never become broker-discoverable merely
    because they are absent from ``tools/list``.
    """
    surface = load_surface()
    advertisable = {name for name in ROUTES if bool(surface.get(name, {}).get("visibleToLlm", False))}
    visible = {entry["name"] for entry in tool_list(env=env)}
    return frozenset(advertisable - visible)


def _search(query: str, env: Mapping[str, str] | None) -> ToolOutcome:
    surface = load_surface()
    needle = query.strip().lower()
    matches = []
    for name in sorted(_hidden_names(env)):
        description = str(surface.get(name, {}).get("description") or "")
        haystack = f"{name} {description}".lower()
        if not needle or needle in haystack:
            matches.append(f"{name}: {description}")
    text = "\n".join(matches) if matches else "no hidden tool matches that query"
    return ToolOutcome(content=({"type": "text", "text": text},))


def _refusal(message: str) -> ToolOutcome:
    error = ClientError(ErrorCode.PAYLOAD_INVALID, message, action=AgentAction.FIX_REQUEST)
    return ToolOutcome(content=(error_block(error),), is_error=True)


def _call(
    name: str, arguments: Mapping[str, Any], dispatcher: Dispatcher, env: Mapping[str, str] | None
) -> ToolOutcome:
    if not name:
        return _refusal("action='call' requires 'name'")
    advertised = {entry["name"] for entry in tool_list(env=env)}
    if name in advertised:
        return _refusal(f"{name!r} is already exposed; call it directly")
    if name not in _hidden_names(env):
        return _refusal(f"unknown or unavailable tool: {name}")
    return dispatcher.call(name, arguments)


def handle(
    arguments: Mapping[str, Any], *, dispatcher: Dispatcher, env: Mapping[str, str] | None = None
) -> ToolOutcome:
    """Run one ``tool`` broker call. The only entry point this module needs."""
    action = str(arguments.get("action") or "")
    if action == "search":
        return _search(str(arguments.get("query") or ""), env)
    if action == "call":
        raw_target_arguments = arguments.get("arguments")
        target_arguments: Mapping[str, Any] = raw_target_arguments if isinstance(raw_target_arguments, Mapping) else {}
        return _call(str(arguments.get("name") or ""), target_arguments, dispatcher, env)
    return _refusal(f"unknown broker action: {action!r}")
