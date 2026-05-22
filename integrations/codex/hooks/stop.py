#!/usr/bin/env python3
"""Codex Stop hook session summary backed by Atelier runtime state."""

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
        from atelier.core.capabilities.plugin_runtime import build_codex_stop_output

        payload = json.loads(sys.stdin.read() or "{}")
        output = build_codex_stop_output(_atelier_root(), payload)
        if not output.get("no_output"):
            print(json.dumps({"systemMessage": output["systemMessage"]}))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
