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


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if payload and payload.get("hook_event_name") not in {None, "SessionStart"}:
            return 0
        from atelier.core.capabilities.plugin_runtime import codex_update_notification

        output = codex_update_notification(
            _atelier_root(),
            current_version=os.environ.get("ATELIER_VERSION", "0.0.0"),
        )
        stdout = output.get("stdout") if isinstance(output, dict) else None
        if stdout:
            print(json.dumps(stdout))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
