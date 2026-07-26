#!/usr/bin/env python3
"""Cursor beforeShellExecution hook: force shell execution through LemonCrow.

Cursor's native terminal tool bypasses the LemonCrow MCP server's `bash` tool
entirely -- with only the MCP server registered, Cursor's agent defaults to
the built-in Read/Grep/Shell tools it already knows rather than a lazily-
discovered MCP one, so LemonCrow's context-compression and caching never
actually engage. This hook denies native shell commands and redirects the
agent to the `bash` tool from the lc MCP server instead.

Loop guard: if the agent retries a native shell call within
``IMMEDIATE_RETRY_SECONDS`` of being denied, it has already gotten (and
likely can't act on, or is stuck on) the nudge -- deny again anyway just
traps it in a tight deny/retry loop burning turns for nothing. So a same-
burst retry is allowed through once, and that permissive state holds for
``COOLOFF_SECONDS`` (default 5 min, override via
LEMONCROW_CURSOR_TOOL_COOLOFF_SECONDS) before enforcement resumes on the next
fresh attempt. State lives in a small per-host-root file, not in-memory --
each hook invocation is a separate process.

Payload/output contract: cursor.com/docs/agent/hooks (beforeShellExecution).
Output only supports permission/user_message/agent_message -- there is no
`updated_input`-style rewrite for this hook (that exists only on the generic
`preToolUse` hook, and even there it edits the input to the *same* native
tool, not a cross-tool redirect) -- deny + a message that names the
replacement tool is the only lever available here.

Fail-open by default (a crash/timeout here still lets the command run) --
this is a nudge, not a sandbox; set `"failClosed": true` on this hook's entry
in hooks.json if a hard guarantee is required.
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


def _enforcement_enabled() -> bool:
    """Deny-and-redirect is opt-in; default OFF. Enable with LEMONCROW_CURSOR_ENFORCE=1.

    Measured on Cursor (multi-turn edit task, 3 runs per arm, billed-token
    accounting): denying the natives does not actually route the agent onto the
    lc MCP tools. The loop guard lets the very next retry through, so the native
    call runs anyway seconds later and the routing benefit is zero. What the
    denied turns do change is which tool the agent reaches for while blocked --
    it falls back to `glob_file_search **/*`, the one orientation tool Cursor
    exposes no hook matcher for (valid matchers are Shell/Read/Write/Grep/
    Delete/Task/MCP:<tool>; there is no Glob). That listed `.venv` and
    `.pytest_cache` and returned ~38K characters in 2 of 3 runs, which every
    later turn re-pays for as cache_read. The run with no denies landed at or
    below the vanilla baseline; the runs with denies cost ~1.8x it.

    So the hooks stay installed but inert unless explicitly switched on.
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
                "user_message": "Native shell blocked — LemonCrow routes execution through its own 'bash' tool.",
                "agent_message": (
                    "Native terminal execution is blocked. Use the 'bash' tool from the lc MCP "
                    "server instead of the terminal for all shell/command execution."
                ),
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
