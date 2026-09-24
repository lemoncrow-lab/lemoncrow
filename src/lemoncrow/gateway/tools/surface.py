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
from lemoncrow.core.environment import mcp_tool_description, mcp_tool_mode, mcp_tool_visible_to_llm

MCP_PROTOCOL_VERSION: Final[str] = "2024-11-05"
SERVER_NAME: Final[str] = "lc"
SERVER_DISPLAY_NAME: Final[str] = "lemoncrow"
SERVER_VERSION: Final[str] = lemoncrow_version

# Injected into hosts that honor MCP initialize instructions. Keep this generic:
# host-specific delegation/tool-name text belongs in the host integration.
SERVER_INSTRUCTIONS: Final[str] = (
    "LemonCrow replaces the grep→read→re-read loop.\n"
    "- Lead with `code_search`: one call = ranked matches (top source inline, "
    "rest as path:Lx-Ly pointers) + related_symbols + candidate_files. Inline "
    "source = already read; never shell-grep or re-verify indexed results.\n"
    "- Known path/symbols → `read`: ONE call, files=[...], exact :Lx-Ly ranges, "
    "never the same file twice. Need, not might-need: a speculative :full costs "
    "more than the turn it saves. Never cat/sed/head/tail.\n"
    "- ALL edits in ONE `edit` edits[] array; prefer {path: 'f.py:Lx-Ly', new} over old/new.\n"
    "- Independent calls (any tools) → ONE message.\n"
    "- `bash` = execution only (tests, git, builds). Large output → a file, never inline prose.\n"
    "- Graphical data (plots, pixel grids, UI) → write a PNG and `read` it; "
    "don't infer visuals from raw bytes."
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
    return mcp_tool_visible_to_llm(tool_name)


def mcp_tool_profile() -> str:
    """Return the requested advertised profile: ``core`` or ``full``."""
    value = os.environ.get("LEMONCROW_MCP_TOOL_PROFILE", "full").strip().lower()
    return value if value in {"core", "full"} else "full"


def tool_profile_exposes(tool_name: str) -> bool:
    """Whether *tool_name* belongs in the selected profile."""
    if mcp_tool_profile() == "core":
        return tool_name in CORE_MCP_TOOLS
    # The broker is unnecessary in full mode and would invite an avoidable call.
    return tool_name != "tool"


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
