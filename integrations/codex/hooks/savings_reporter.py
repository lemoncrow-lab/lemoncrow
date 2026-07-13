#!/usr/bin/env python3
"""Codex PostToolUse savings reporter backed by LemonCrow runtime state."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    if root:
        return Path(root)
    return Path.home() / ".lemoncrow"


def main() -> int:
    try:
        from lemoncrow.core.capabilities.plugin_runtime import (
            build_codex_post_tool_use_ledger_output,
            build_codex_post_tool_use_savings_output,
        )

        payload = json.loads(sys.stdin.read() or "{}")
        root = _lemoncrow_root()
        # Savings telemetry stays silent (state only), except for the native-
        # tool nudge it returns the first time a given native tool call is seen
        # (Codex has no permission-deny like Claude, so this is the only
        # correction back to lc.*). The run ledger + tool-supervision capture
        # also happen here; its only surfaced output is the repeat-failure
        # nudge -- Codex has no separate PostToolUseFailure event, so it is
        # folded into PostToolUse. Both messages can fire on the same call.
        savings = build_codex_post_tool_use_savings_output(root, payload)
        ledger = build_codex_post_tool_use_ledger_output(root, payload)
        messages = []
        nudge_message = savings.get("message")
        if isinstance(nudge_message, str) and nudge_message.strip():
            parts = [nudge_message.strip()]
            context = savings.get("additionalContext")
            if isinstance(context, str) and context.strip():
                parts.append(context.strip())
            messages.append("\n".join(parts))
        ledger_message = ledger.get("systemMessage")
        if isinstance(ledger_message, str) and ledger_message.strip():
            messages.append(ledger_message.strip())
        if messages:
            sys.stdout.write(json.dumps({"systemMessage": "\n\n".join(messages)}) + "\n")
    except Exception:  # noqa: BLE001 - lifecycle hooks must be fail-open
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
