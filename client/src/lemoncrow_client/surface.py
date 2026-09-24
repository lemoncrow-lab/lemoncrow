"""The advertised tool surface: names, descriptions and input schemas.

The client must advertise the tools before it knows whether the server is
reachable -- ``tools/list`` is answered during MCP initialization, and the
degradation contract says a session with no server still has working
client-side tools. Fetching the surface from the server would make an offline
session advertise nothing at all, which is a worse failure than the one it was
meant to avoid.

So the surface ships as data: ``toolsurface.json``, generated from the public
``lemoncrow`` MCP registry. A JSON file is more auditable than code that builds
one, it is identical on every machine, and it cannot drift silently --
``tests/test_tool_surface.py`` regenerates it from the real registry whenever
the public package is importable and fails on any difference.

Advertised names are **unchanged**. ``mcp__lc__read`` is still
``mcp__lc__read`` whether ``read`` runs here or on the server. The bundled data
also carries descriptors for the public registry's process-spawning tools, but
``tool_list`` emits only names present in ``ROUTES``; the one-process policy
therefore excludes ``mcp``, ``agent`` and ``workflow`` by construction.

Static LLM visibility and core-profile membership are generated into the
bundled descriptor from ``lemoncrow.core.environment``. The dependency-free
client consumes those bits instead of carrying a second visibility policy.
``LEMONCROW_MCP_TOOL_PROFILE=core`` may narrow the already-visible set further;
``full`` means all tools the canonical allowlist says are visible, not every
routed tool. There is no runtime hide/subtract mechanism: model-facing exposure
is controlled only by the generated allowlist/profile data.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from .routing import ROUTES

__all__ = ["SURFACE_PATH", "ToolSpec", "load_surface", "tool_list"]

SURFACE_PATH: Final[Path] = Path(__file__).with_name("toolsurface.json")


class ToolSpec(dict[str, Any]):
    """One entry of the MCP ``tools/list`` answer."""


def load_surface(path: Path | None = None) -> Mapping[str, Mapping[str, Any]]:
    """Read the bundled descriptor. Raises if it is missing or malformed.

    Deliberately not forgiving: a client that silently advertised an empty tool
    list because a data file failed to parse would look to the host exactly
    like a client whose server is down, and the two need different fixes.
    """
    target = path or SURFACE_PATH
    raw = json.loads(target.read_text(encoding="utf-8"))
    tools = raw.get("tools") if isinstance(raw, Mapping) else None
    if not isinstance(tools, Mapping) or not tools:
        raise ValueError(f"{target} carries no tool definitions")
    return {str(name): spec for name, spec in tools.items() if isinstance(spec, Mapping)}


def _mcp_tool_profile(env: Mapping[str, str] | None = None) -> str:
    """``LEMONCROW_MCP_TOOL_PROFILE`` -- ``core`` selects generated core tools.

    Same env var name as the private server's own profile switch
    (``lemoncrow.gateway.adapters.mcp_server._mcp_tool_profile``), so one host
    config value works whichever binary ends up answering ``tools/list``.
    Any value other than ``core`` -- including unset -- keeps the full surface,
    which was the only behaviour before this existed.
    """
    values = os.environ if env is None else env
    value = values.get("LEMONCROW_MCP_TOOL_PROFILE", "full").strip().lower()
    return value if value in {"core", "full"} else "full"


def tool_list(path: Path | None = None, *, env: Mapping[str, str] | None = None) -> list[ToolSpec]:
    """The MCP ``tools/list`` payload, in name order.

    Every routed tool that the generated canonical policy marks visible appears
    exactly once. ``LEMONCROW_MCP_TOOL_PROFILE=core`` may narrow that visible set
    to generated core members. A routed tool with no
    bundled description still appears -- with its name and a permissive schema
    -- because dropping it would change the surface the agent sees, and the
    whole requirement is that the surface changes only for the profile and
    hide-list the caller actually asked for.
    """
    surface = load_surface(path)
    names = [name for name in sorted(ROUTES) if bool(surface.get(name, {}).get("visibleToLlm", False))]
    if _mcp_tool_profile(env) == "core":
        names = [name for name in names if bool(surface.get(name, {}).get("core", False))]
    out: list[ToolSpec] = []
    for name in names:
        spec = surface.get(name, {})
        description = str(spec.get("description") or f"LemonCrow {name} tool.")
        schema = spec.get("inputSchema")
        out.append(
            ToolSpec(
                {
                    "name": name,
                    "description": description,
                    "inputSchema": dict(schema) if isinstance(schema, Mapping) else {"type": "object"},
                }
            )
        )
    return out
