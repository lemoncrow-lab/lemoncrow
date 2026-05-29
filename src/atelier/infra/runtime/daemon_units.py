"""Daemon unit/label constants + platform-detection helpers (Phase 25-03).

Moved verbatim from ``gateway/cli/app.py``. The systemd unit names, launchd
labels, user-unit directories, and default stack host/port values are
OS-registered identifiers; renaming them would orphan already-installed units,
so they are relocated byte-for-byte (no rename). ``_is_macos``/``_is_linux``/
``_subprocess_output``/``_systemd_user_bus_unavailable`` are part of the
in-flight systemd-bus WIP and are copied without behaviour changes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SUPPORTED_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS = ("today", "week", "month")
DEFAULT_SERVICECTL_EXTERNAL_ANALYTICS_PERIODS = (
    "today",
    "week",
    "month",
)

CONTROLLER_UNIT = "atelier-controller.service"
STACK_UNIT = "atelier-stack.service"
LETTA_UNIT = "atelier-letta.service"
OPENMEMORY_UNIT = "atelier-openmemory.service"
ZOEKT_UNIT = "atelier-zoekt.service"
MCP_UNIT = "atelier-mcp.service"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
LAUNCHD_USER_DIR = Path.home() / "Library" / "LaunchAgents"
CONTROLLER_LABEL = "com.atelier.controller"
STACK_LABEL = "com.atelier.stack"
LETTA_LABEL = "com.atelier.letta"
OPENMEMORY_LABEL = "com.atelier.openmemory"
ZOEKT_LABEL = "com.atelier.zoekt"
MCP_LABEL = "com.atelier.mcp"
DEFAULT_STACK_SERVICE_HOST = "0.0.0.0"
DEFAULT_STACK_SERVICE_PORT = 8787
DEFAULT_STACK_FRONTEND_HOST = "0.0.0.0"
DEFAULT_STACK_FRONTEND_PORT = 3125


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _subprocess_output(result: Any) -> str:
    return "\n".join(part for part in (getattr(result, "stdout", ""), getattr(result, "stderr", "")) if part)


def _systemd_user_bus_unavailable(output: str) -> bool:
    markers = (
        "Failed to connect to user scope bus",
        "$DBUS_SESSION_BUS_ADDRESS",
        "$XDG_RUNTIME_DIR",
        "No medium found",
    )
    return any(marker in output for marker in markers)
