#!/usr/bin/env python3
"""Codex PreToolUse discipline hook backed by LemonCrow runtime state."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    return Path(root) if root else Path.home() / ".lemoncrow"


def main() -> int:
    try:
        from lemoncrow.core.capabilities.plugin_runtime import build_codex_pre_tool_use_output

        payload = json.loads(sys.stdin.read() or "{}")
        output = build_codex_pre_tool_use_output(_lemoncrow_root(), payload)
        hook_output = output.get("hookSpecificOutput")
        if isinstance(hook_output, dict):
            sys.stdout.write(json.dumps({"hookSpecificOutput": hook_output}) + "\n")
    except Exception:  # noqa: BLE001 - lifecycle hooks must be fail-open
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
