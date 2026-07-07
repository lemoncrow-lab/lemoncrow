#!/usr/bin/env python3
"""Codex SessionStart update notifier backed by Atelier runtime state."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _atelier_root() -> Path:
    root = os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT")
    if root:
        return Path(root)
    return Path.home() / ".atelier"


def _session_state_path(cwd: str | None = None) -> Path:
    # Canonical hashing lives in atelier.core.foundation.paths.workspace_key --
    # import it rather than keeping a local copy in sync by hand.
    from atelier.core.foundation.paths import workspace_key

    workspace = cwd or os.environ.get("CODEX_WORKSPACE_ROOT") or os.getcwd()
    h = workspace_key(workspace)
    return _atelier_root() / "workspaces" / h / "session_state.json"


def _write_session_state(session_id: str, cwd: str | None = None) -> None:
    p = _session_state_path(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        state: dict = json.loads(p.read_text("utf-8")) if p.exists() else {}
    except (json.JSONDecodeError, OSError):
        state = {}
    state["session_id"] = session_id
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if payload and payload.get("hook_event_name") not in {None, "SessionStart"}:
            return 0

        # Bridge host session_id into session_state.json so MCP server live
        # savings events use the same ID as the stop/savings hooks.
        session_id = str(payload.get("session_id") or "")
        cwd = str(payload.get("cwd") or "")
        if session_id:
            _write_session_state(session_id, cwd or None)

        # Check for update notification from daemon/MCP auto-update
        state_path = _atelier_root() / "update_state.json"
        if state_path.exists():
            update_data = json.loads(state_path.read_text("utf-8"))
            if (
                isinstance(update_data, dict)
                and update_data.get("current_version")
                and update_data.get("previous_version")
                and update_data["current_version"] != update_data["previous_version"]
                and not update_data.get("notified")
            ):
                prev_ver = update_data["previous_version"]
                cur_ver = update_data["current_version"]
                method = update_data.get("method", "auto")
                msg = (
                    f"Atelier updated from {prev_ver} → {cur_ver} (via {method}). "
                    "Release notes: https://github.com/atelier-ws/atelier/releases"
                )
                sys.stdout.write(json.dumps({"systemMessage": msg}) + "\n")
                sys.stdout.flush()
                # Mark as notified
                update_data["notified"] = True
                state_path.write_text(json.dumps(update_data, indent=2), encoding="utf-8")
    except (ImportError, json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
