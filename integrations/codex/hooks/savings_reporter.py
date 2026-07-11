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
        # Savings + native-tool telemetry stay silent (state only). The run
        # ledger + tool-supervision capture also happen here. The only surfaced
        # output is the repeat-failure nudge -- Codex has no separate
        # PostToolUseFailure event, so it is folded into PostToolUse.
        build_codex_post_tool_use_savings_output(root, payload)
        ledger = build_codex_post_tool_use_ledger_output(root, payload)
        message = ledger.get("systemMessage")
        if isinstance(message, str) and message.strip():
            sys.stdout.write(json.dumps({"systemMessage": message}) + "\n")
    except Exception:  # noqa: BLE001 - lifecycle hooks must be fail-open
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
