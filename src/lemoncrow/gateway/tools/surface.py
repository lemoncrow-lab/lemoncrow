"""Canonical policy for LemonCrow's advertised tool surface.

This module is intentionally transport-independent. MCP stdio/HTTP, the
standalone service API, CLI inspection, and owned-agent frontends may all need
the same server identity, instructions, tool descriptions, and profile rules.
Execution and JSON-RPC dispatch stay elsewhere.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any, Final

from lemoncrow import __version__ as lemoncrow_version
from lemoncrow.core.environment import CORE_MCP_TOOLS as CORE_MCP_TOOLS
from lemoncrow.core.environment import REVIEW_AUTONOMOUS_MCP_TOOLS as REVIEW_AUTONOMOUS_MCP_TOOLS
from lemoncrow.core.environment import REVIEW_MCP_TOOLS as REVIEW_MCP_TOOLS
from lemoncrow.core.environment import mcp_tool_description, mcp_tool_mode, mcp_tool_visible_to_llm

MCP_PROTOCOL_VERSION: Final[str] = "2024-11-05"
SERVER_NAME: Final[str] = "lc"
SERVER_DISPLAY_NAME: Final[str] = "lemoncrow"
SERVER_VERSION: Final[str] = lemoncrow_version

# Injected into hosts that honor MCP initialize instructions. Keep this generic:
# host-specific delegation/tool-name text belongs in the host integration.
SERVER_INSTRUCTIONS: Final[str] = (
    "Git source discovery → `code_search` first; combine terms. Returns ranked inline "
    "source, path:Lx-Ly pointers and related symbols. Don't embed source grep/rg "
    "in compound Bash.\n"
    "Known files/symbols → `read` once with exact ranges. Batch edits in one `edit` "
    "call and independent tool calls together.\n"
    "`bash` for Git, tests, builds, logs, non-Git searches and exact shell output. "
    "Index misses fall back to grep. Large output → file; visuals → PNG then `read`."
)

# Direct host integrations can advertise this eager core without making normal
# coding operations pay a broker/search round trip. The membership itself lives
# in core.environment so every transport and generated client surface shares one
# static policy owner.
TOOL_BROKER_DESCRIPTION: Final[str] = (
    "Deterministic fallback for rare LemonCrow tools hidden by the core profile. "
    "Normal code_search/read/edit/bash/web_fetch calls are already exposed: never "
    "search for those. Use search once for a rare capability, then call its exact name."
)
TOOL_BROKER_SURFACE_SPEC: Final[dict[str, Any]] = {
    "name": "tool",
    "description": TOOL_BROKER_DESCRIPTION,
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["search", "call"]},
            "query": {"type": "string"},
            "name": {"type": "string"},
            "arguments": {"type": "object"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def tool_description(spec: dict[str, Any]) -> str:
    """Return the advertised description for one registered tool."""
    base = mcp_tool_description(
        str(spec.get("name", "") or ""),
        str(spec.get("description", "") or ""),
    )
    if spec.get("name") == "mcp":
        # Enrich from an existing disk cache only. Never spawn/configure servers
        # while merely rendering the tool surface.
        with contextlib.suppress(Exception):
            from lemoncrow.gateway.adapters import mcp_proxy

            names = mcp_proxy.cached_server_names()
            if names:
                base = f"{base}\n\nLast known configured servers: {', '.join(names)}."
    return base


def tool_visible_to_llm(tool_name: str) -> bool:
    """Apply the canonical environment/benchmark visibility policy."""
    profile = mcp_tool_profile()
    if profile == "review":
        return tool_name in REVIEW_MCP_TOOLS
    if profile == "review_autonomous":
        return tool_name in REVIEW_AUTONOMOUS_MCP_TOOLS
    return mcp_tool_visible_to_llm(tool_name)


def mcp_tool_profile() -> str:
    """Return the requested advertised profile: core, review, review_autonomous, or full."""
    value = os.environ.get("LEMONCROW_MCP_TOOL_PROFILE", "full").strip().lower()
    return value if value in {"core", "review", "review_autonomous", "full"} else "full"


def tool_profile_exposes(tool_name: str) -> bool:
    """Whether *tool_name* belongs in the selected profile."""
    profile = mcp_tool_profile()
    if profile == "core":
        return tool_name in CORE_MCP_TOOLS
    if profile == "review":
        return tool_name in REVIEW_MCP_TOOLS
    if profile == "review_autonomous":
        return tool_name in REVIEW_AUTONOMOUS_MCP_TOOLS
    # The broker is unnecessary in full mode and would invite an avoidable call.
    return tool_name != "tool"


def tool_profile_allows_call(tool_name: str) -> bool:
    """Enforce hard-call authority for security-sensitive profiles.

    Core/review/full preserve the existing exact-name compatibility contract.
    Autonomous Review is different: repository or prompt content must not be able
    to invoke a hidden registered capability merely by knowing its name.
    """

    profile = mcp_tool_profile()
    return profile != "review_autonomous" or tool_name in REVIEW_AUTONOMOUS_MCP_TOOLS


def tool_mode(spec: dict[str, Any]) -> str:
    """Return the operational mode displayed by status/inspection surfaces."""
    return mcp_tool_mode(str(spec.get("name", "") or ""))


def bundled_tool_record(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Build the dependency-free thin-client descriptor from canonical policy.

    ``lemoncrow-client`` intentionally cannot import this package at runtime.
    Static visibility therefore crosses the package boundary as generated data,
    not as a duplicated Python constant or runtime subtraction mechanism.
    """
    return {
        "description": tool_description(spec),
        "inputSchema": spec.get("inputSchema", {}),
        "visibleToLlm": mcp_tool_mode(name) != "hidden",
        "core": name in CORE_MCP_TOOLS,
    }
