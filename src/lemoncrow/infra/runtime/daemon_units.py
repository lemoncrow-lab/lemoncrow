"""Daemon unit/label constants + platform-detection helpers (Phase 25-03).

Shared systemd/launchd helpers for the supported persistent MCP tunnel.
Retired controller/stack identifiers intentionally live only in the temporary
migration cleanup script, never in the active runtime.
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

SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
LAUNCHD_USER_DIR = Path.home() / "Library" / "LaunchAgents"
MCP_LABEL = "com.lemoncrow.mcp"


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
