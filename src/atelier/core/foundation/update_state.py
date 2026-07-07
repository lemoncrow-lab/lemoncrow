"""Update-state helpers for Atelier auto-update notifications.

Writes and reads a small JSON file at ``~/.atelier/update_state.json`` so
that SessionStart hooks can detect when Atelier was updated and notify the
user.  The daemon (servicectl) and MCP server are the primary writers; the
hooks are the primary readers.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _root() -> Path:
    return Path(os.environ.get("ATELIER_ROOT") or (Path.home() / ".atelier"))


def update_state_path(root: Path | None = None) -> Path:
    return (root or _root()) / "update_state.json"


def read_update_state(root: Path | None = None) -> dict[str, Any]:
    p = update_state_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def write_update_state(
    *,
    previous_version: str,
    current_version: str,
    method: str,
    root: Path | None = None,
) -> None:
    """Record a completed update so SessionStart hooks can notify the user."""
    data = {
        "previous_version": previous_version,
        "current_version": current_version,
        "updated_at": datetime.now(UTC).isoformat(),
        "method": method,
        "notified": False,
    }
    p = update_state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def acknowledge_update_notification(root: Path | None = None) -> None:
    """Mark the current update notification as acknowledged (notified=True)."""
    state = read_update_state(root)
    if state and not state.get("notified"):
        state["notified"] = True
        p = update_state_path(root)
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")


__all__ = [
    "acknowledge_update_notification",
    "read_update_state",
    "update_state_path",
    "write_update_state",
]
