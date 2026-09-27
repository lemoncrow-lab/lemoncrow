"""Canonical access to LemonCrow's registered tool handlers.

Tool registration is owned by independently importable handler modules. The
legacy ``mcp_server`` composition root is imported last only to install the
process-default tool runtime hooks while that composition is still being
extracted; registry population itself no longer depends on the monolith.
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from lemoncrow.gateway.tool_registry_state import REGISTERED_TOOLS as _TOOLS
from lemoncrow.gateway.tools.surface import (
    TOOL_BROKER_SURFACE_SPEC,
    mcp_tool_profile,
    tool_description,
    tool_profile_exposes,
    tool_visible_to_llm,
)

_BOOTSTRAP_LOCK = threading.Lock()
_BOOTSTRAPPED = False


def _ensure_registered() -> None:
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAPPED:
            return
        # Import every independently-owned handler module. All decorators write
        # into the same dependency-minimal REGISTERED_TOOLS object, so registry
        # population itself does not require the MCP protocol/composition module.
        from lemoncrow.gateway.adapters.mcp import bash as _bash  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_code_intel as _tools_code_intel  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_commodity as _tools_commodity  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_compact as _tools_compact  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_context as _tools_context  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_edit as _tools_edit  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_memory as _tools_memory  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_orchestration as _tools_orchestration  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_read as _tools_read  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_rescue as _tools_rescue  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_review as _tools_review  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_search as _tools_search  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_statusline as _tools_statusline  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_trace as _tools_trace  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_utility as _tools_utility  # noqa: F401
        from lemoncrow.gateway.adapters.mcp import tools_verify as _tools_verify  # noqa: F401

        # Temporary compatibility composition only: this installs the default
        # runtime hook factory used by synchronous payload/server execution.
        # Tool registration is complete before this side-effect import.
        importlib.import_module("lemoncrow.gateway.adapters.mcp_server")

        _BOOTSTRAPPED = True


def registered_tools() -> dict[str, dict[str, Any]]:
    """Return the one live tool registry, bootstrapping handlers if needed."""
    _ensure_registered()
    return _TOOLS


def tool_spec(name: str) -> dict[str, Any] | None:
    """Return one registered tool specification by exact name."""
    return registered_tools().get(name)


def call_registered_tool(name: str, arguments: dict[str, Any]) -> Any:
    """Invoke one canonical registered tool handler by exact name."""
    spec = tool_spec(name)
    if spec is None:
        raise KeyError(name)
    handler = spec.get("handler")
    if not callable(handler):
        raise KeyError(name)
    return handler(arguments)


def registered_tool_names() -> frozenset[str]:
    """Snapshot the currently registered exact tool names."""
    return frozenset(registered_tools())


def advertised_tools(
    *,
    visibility: Callable[[str, dict[str, Any]], bool] | None = None,
    description: Callable[[dict[str, Any]], str] | None = None,
) -> list[dict[str, Any]]:
    """Return the canonical ``tools/list`` payload for the active profile.

    ``visibility`` and ``description`` are injectable only for compatibility
    with embedded callers that override the legacy mcp_server policy at runtime.
    Normal transports use the canonical surface policy directly.
    """
    visible = visibility or (lambda name, _spec: tool_visible_to_llm(name))
    describe = description or tool_description
    tools = [
        {
            "name": name,
            "description": describe(spec),
            "inputSchema": spec.get("inputSchema", {}),
        }
        for name, spec in registered_tools().items()
        if tool_profile_exposes(name) and visible(name, spec)
    ]
    if mcp_tool_profile() == "core":
        tools.append(deepcopy(TOOL_BROKER_SURFACE_SPEC))
    tools.sort(key=lambda item: str(item["name"]))
    return tools


__all__ = ["advertised_tools", "call_registered_tool", "registered_tool_names", "registered_tools", "tool_spec"]
