#!/usr/bin/env python3
"""Codex Stop hook session summary backed by LemonCrow runtime state."""

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
        from lemoncrow.core.capabilities.plugin_runtime import build_codex_stop_output

        payload = json.loads(sys.stdin.read() or "{}")
        output = build_codex_stop_output(_lemoncrow_root(), payload)
        if not output.get("no_output"):
            sys.stdout.write(json.dumps({"systemMessage": output["systemMessage"]}) + "\n")
    except (ImportError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
