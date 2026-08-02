#!/usr/bin/env python3
"""Cursor beforeShellExecution hook: force shell through LemonCrow bash.

Modes via LEMONCROW_CURSOR_ENFORCE:
  unset/0 — allow (default)
  1/soft  — deny with cooloff (legacy IDE nudge)
  hard    — stick deny, no cooloff (bench)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

IMMEDIATE_RETRY_SECONDS = 10
COOLOFF_SECONDS = int(os.environ.get("LEMONCROW_CURSOR_TOOL_COOLOFF_SECONDS", "300"))


def _lemoncrow_root() -> Path:
    return Path(
        os.environ.get("LEMONCROW_ROOT", "") or os.environ.get("LEMONCROW_STORE_ROOT", "") or Path.home() / ".lemoncrow"
    )


def _state_path() -> Path:
    return _lemoncrow_root() / "cursor-hooks" / "shell_deny_state.json"


def _enforce_mode() -> str:
    raw = os.environ.get("LEMONCROW_CURSOR_ENFORCE", "0").strip().lower()
    if raw in {"hard", "strict", "bench"}:
        return "hard"
    if raw in {"1", "true", "yes", "on", "soft"}:
        return "soft"
    return ""


def _decide() -> str:
    now = time.time()
    path = _state_path()
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(state, dict):
            state = {}
    except (OSError, json.JSONDecodeError):
        state = {}

    last_at = state.get("last_event_at")
    last_action = state.get("last_action")
    action = "deny"
    if isinstance(last_at, (int, float)):
        age = now - last_at
        if last_action == "deny" and age < IMMEDIATE_RETRY_SECONDS:
            action = "allow"
        elif last_action == "allow" and age < COOLOFF_SECONDS:
            action = "allow"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_event_at": now, "last_action": action}), encoding="utf-8")
    except OSError:
        pass
    return action


def main() -> int:
    try:
        json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        pass

    mode = _enforce_mode()
    if not mode:
        sys.stdout.write(json.dumps({"permission": "allow"}) + "\n")
        return 0

    if mode == "soft" and _decide() == "allow":
        sys.stdout.write(json.dumps({"permission": "allow"}) + "\n")
        return 0

    sys.stdout.write(
        json.dumps(
            {
                "permission": "deny",
                "user_message": "Native shell blocked — LemonCrow routes execution through its own 'bash' tool.",
                "agent_message": (
                    "Native terminal execution is blocked. Use the 'bash' tool from the lemoncrow MCP "
                    "server (MCP:bash) instead. Do not retry the native Shell tool."
                ),
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
