#!/usr/bin/env python3
"""Cursor beforeReadFile hook: force file reads through LemonCrow.

Same rationale as before_shell_execution.py -- without this, Cursor's agent
defaults to its own built-in Read tool and never discovers/uses the `read`
tool the LemonCrow MCP server exposes (cached re-reads, symbol-scoped reads,
multi-mode projections), so the server's context-compression story never
actually engages.

Same loop guard as before_shell_execution.py (see that file for the full
rationale): an immediate same-burst retry after a deny is allowed through
once, and that permissive state holds for a cooloff window before
enforcement resumes. Tracked in a separate state file so a shell retry loop
and a read retry loop don't interfere with each other's cooloff.

beforeReadFile's output contract has no `agent_message` field (cursor.com/
docs/agent/hooks) -- only `permission` and a user-facing `user_message` -- so
the agent has to infer the replacement tool from its own tool list, or from
the `lemoncrow.tools.mdc` project rule when one is installed (workspace
installs only; Cursor has no scriptable file-based mechanism for global/User
Rules, only a Customize-UI setting, so global installs rely on the sibling
shell hook's agent_message plus the model's own MCP tool discovery).

Fail-open by default -- see before_shell_execution.py for the failClosed note.
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
    return _lemoncrow_root() / "cursor-hooks" / "read_deny_state.json"


def _enforcement_enabled() -> bool:
    """Deny-and-redirect is opt-in; default OFF. Enable with LEMONCROW_CURSOR_ENFORCE=1.

    See before_shell_execution.py for the measurement this default comes from:
    denying natives buys no routing (the loop guard lets the retry through) but
    pushes the agent onto `glob_file_search **/*` -- the one orientation tool
    Cursor has no hook matcher for -- which dumped ~38K chars of `.venv` listing
    into context and roughly doubled the re-billed tokens.
    """
    return os.environ.get("LEMONCROW_CURSOR_ENFORCE", "0").strip().lower() in {"1", "true", "yes", "on"}


def _decide() -> str:
    """Return "deny" or "allow" per the loop-guard state machine above.

    Sliding window, no fixed expiry: as long as calls keep arriving inside
    ``COOLOFF_SECONDS`` of the last one, the permissive state keeps renewing.
    Enforcement only resumes once the calls actually stop for a while.
    """
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
            action = "allow"  # same-burst retry right after a deny -> break the loop
        elif last_action == "allow" and age < COOLOFF_SECONDS:
            action = "allow"  # still inside the sliding cooloff window

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

    if not _enforcement_enabled() or _decide() == "allow":
        sys.stdout.write(json.dumps({"permission": "allow"}) + "\n")
        return 0

    sys.stdout.write(
        json.dumps(
            {
                "permission": "deny",
                "user_message": "Native file read blocked — LemonCrow routes reads through its own 'read' tool.",
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
