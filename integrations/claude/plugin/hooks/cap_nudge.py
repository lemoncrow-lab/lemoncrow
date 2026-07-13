#!/usr/bin/env python3
"""PostToolUse hook — one-shot user-facing savings-cap nudge (Claude).

When the plan's savings cap is exhausted, the plugin degrades to host defaults
(never blocks). This hook surfaces a single user-only notice per session via the
``systemMessage`` field — which Claude Code shows to the human but does NOT add
to the model context — so it costs no tokens and cannot derail the agent. The
tool output passes through unchanged (no ``updatedToolOutput``/``decision``).

Fail-open: any error exits 0 silently — never blocks or disrupts a tool call.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    return Path(root) if root else Path.home() / ".lemoncrow"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, TypeError):
        return 0
    try:
        from lemoncrow.core.capabilities.plugin_runtime import build_cap_nudge

        session_id = str(payload.get("session_id") or "_global")
        message = build_cap_nudge(_root(), session_id=session_id, host="claude")
        if message:
            # systemMessage -> user only; suppressOutput hides this hook's stdout
            # from the transcript. Tool result is untouched.
            sys.stdout.write(json.dumps({"systemMessage": message, "suppressOutput": True}) + "\n")
    except Exception:  # noqa: BLE001 — fail-open
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
