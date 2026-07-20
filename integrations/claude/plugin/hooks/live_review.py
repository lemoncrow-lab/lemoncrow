#!/usr/bin/env python3
"""PostToolUse hook — non-blocking trigger for the live/automated reviewer.

Fires after Edit/Write/MultiEdit, AFTER post_tool_use.py has recorded the
``file_edit`` event. Does only cheap work: load reviewer settings, count edits,
and (when enabled) detach a reviewer child that runs the actual review
out-of-band. Returns 0 immediately — never blocks the turn. Fail-open.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


def _session_state_path() -> Path:
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    return Path(workspace).expanduser().resolve() / ".lemoncrow" / "workspace" / "session_state.json"


def _read_session_state() -> dict:  # type: ignore[type-arg]
    p = _session_state_path()
    try:
        return json.loads(p.read_text("utf-8")) if p.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    if root:
        return Path(root)
    state = _read_session_state()
    if state.get("lemoncrow_root"):
        return Path(state["lemoncrow_root"])
    return Path.home() / ".lemoncrow"


def _spawn(session_id: str, mode: str, path: str, root: Path) -> None:
    """Detach a reviewer child. Never waits — returns control immediately."""
    override = os.environ.get("LEMONCROW_REVIEWER_CHILD_CMD")
    cmd = (
        shlex.split(override) if override else [sys.executable, "-m", "lemoncrow.pro.capabilities.live_reviewer.child"]
    )
    cmd += ["--session", session_id, "--mode", mode, "--path", path, "--root", str(root)]
    env = dict(os.environ)
    env["LEMONCROW_IN_REVIEW"] = "1"
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=env,
    )


def _dormant() -> bool:
    try:
        from lemoncrow.core.capabilities.plugin_runtime import cap_exhausted

        return bool(cap_exhausted(_lemoncrow_root()))
    except Exception:
        return False


def main() -> int:
    # A reviewer's own activity must never trigger another reviewer.
    if os.environ.get("LEMONCROW_IN_REVIEW"):
        return 0
    if _dormant():
        return 0  # dormant: no live review (a Pro capability)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        return 0

    tool_name = payload.get("tool_name", "") or ""
    if tool_name not in ("Edit", "Write", "MultiEdit"):
        return 0
    tool_input = payload.get("tool_input", {}) or {}
    edited = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("filename") or ""
    if not edited:
        return 0

    try:
        from lemoncrow.core.foundation.paths import session_dir
        from lemoncrow.pro.capabilities.live_reviewer.edit_counter import count_file_edits
        from lemoncrow.pro.capabilities.live_reviewer.settings import load_reviewer_settings

        root = _lemoncrow_root()
        settings = load_reviewer_settings(root)
        if not settings.enabled:
            return 0
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return 0
        count = count_file_edits(session_dir(root, "claude", session_id) / "run.json")
        if settings.deep_edit_count_reviewer and count > 0 and count % settings.deep_edit_count_interval == 0:
            _spawn(session_id, "deep", edited, root)
        elif settings.live_reviewer:
            _spawn(session_id, "live", edited, root)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
