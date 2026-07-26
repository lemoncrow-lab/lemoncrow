#!/usr/bin/env python3
"""Cursor preToolUse hook: force Grep/Glob/Write/StrReplace/Delete through LemonCrow.

beforeShellExecution and beforeReadFile cover Shell and Read, but Cursor's
native tool palette also has separate Grep, Glob, Write, StrReplace, and
Delete tools that neither of those hooks touches -- left alone, an agent can
route straight around LemonCrow's `code_search`/`edit`/`bash` tools through
any of those five instead. `preToolUse` is the generic hook that covers
every built-in tool (matcher values: Shell, Read, Write, Grep, Delete, Task,
and MCP:<tool> -- cursor.com/docs/agent/hooks); this project's hooks.json
scopes it via `matcher` to just the five gap tools, so Shell/Read stay owned
by their own dedicated hooks.

Same sliding-window loop guard as before_shell_execution.py/
before_read_file.py (see before_shell_execution.py for the full rationale),
in its own state file so it doesn't share a cooloff with those two.

preToolUse's output also supports `updated_input` (rewrite the input to the
*same* native tool) but nothing that redirects to a different tool entirely
-- deny + a message naming the lc replacement is still the only cross-tool
lever. `permission: "ask"` is accepted by the schema but not enforced for
preToolUse today (per Cursor's docs), so only allow/deny are used here.

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

_REPLACEMENT = {
    "Grep": "code_search",
    "Glob": "code_search",
    "Write": "edit",
    "StrReplace": "edit",
    "Delete": "bash",
}


def _lemoncrow_root() -> Path:
    return Path(
        os.environ.get("LEMONCROW_ROOT", "") or os.environ.get("LEMONCROW_STORE_ROOT", "") or Path.home() / ".lemoncrow"
    )


def _state_path() -> Path:
    return _lemoncrow_root() / "cursor-hooks" / "pretooluse_deny_state.json"


def _enforcement_enabled() -> bool:
    """Deny-and-redirect is opt-in; default OFF. Enable with LEMONCROW_CURSOR_ENFORCE=1.

    See before_shell_execution.py for the measurement this default comes from.
    This hook is also the one that cannot be made complete on Cursor: the matcher
    vocabulary has no `Glob`, so `glob_file_search` can never be covered -- and
    that is precisely the tool a blocked agent escapes to. Denying the rest while
    leaving the most expensive one open is strictly worse than denying nothing.
    """
    return os.environ.get("LEMONCROW_CURSOR_ENFORCE", "0").strip().lower() in {"1", "true", "yes", "on"}


def _decide() -> str:
    """Return "deny" or "allow" per the loop-guard state machine (sliding window, no fixed expiry)."""
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
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    tool_name = str(payload.get("tool_name") or "")
    replacement = _REPLACEMENT.get(tool_name, "bash")

    if not _enforcement_enabled() or _decide() == "allow":
        sys.stdout.write(json.dumps({"permission": "allow"}) + "\n")
        return 0

    sys.stdout.write(
        json.dumps(
            {
                "permission": "deny",
                "user_message": (
                    f"Native {tool_name or 'tool'} blocked — LemonCrow routes this through its own "
                    f"'{replacement}' tool."
                ),
                "agent_message": (
                    f"Native '{tool_name}' is blocked. Use the '{replacement}' tool from the lc MCP " "server instead."
                ),
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
