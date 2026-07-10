"""Update-state helpers for Atelier auto-update notifications.

Writes and reads a small JSON file at ``~/.atelier/update_state.json`` so
that SessionStart hooks can detect when Atelier was updated and notify the
user.  The daemon (servicectl) and MCP server are the primary writers; the
hooks are the primary readers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_VERSION_RE = re.compile(r"\bversion\s+([0-9][^\s]*)")
# Bin dirs to append to PATH when resolving the `atelier` executable, for
# callers spawned by launchd/systemd with a minimal PATH (e.g. the servicectl
# daemon) that would otherwise never find a user-installed `atelier`.
_COMMON_ATELIER_BIN_DIRS = (
    str(Path.home() / ".local" / "share" / "uv" / "tools" / "atelier" / "bin"),
    str(Path.home() / ".atelier" / "uv-tools" / "atelier" / "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def installed_cli_version() -> str | None:
    """Return the version reported by the installed ``atelier`` executable.

    Queries the CLI itself (not the current process's in-memory package
    metadata) so callers see the version actually on disk after a
    reinstall/update, even when the calling process is stale. The PATH used
    to resolve ``atelier`` is augmented with common install locations so
    this also works for callers (e.g. the servicectl daemon under launchd)
    whose inherited PATH is minimal. Returns ``None`` if the binary can't be
    resolved or fails to report a version.
    """
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([env.get("PATH", ""), *_COMMON_ATELIER_BIN_DIRS])
    try:
        result = subprocess.run(["atelier", "--version"], capture_output=True, text=True, timeout=15, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(result.stdout)
    return match.group(1) if result.returncode == 0 and match else None


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
    "installed_cli_version",
    "read_update_state",
    "update_state_path",
    "write_update_state",
]
