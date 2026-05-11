#!/usr/bin/env python3
"""Lifecycle hook that maintains Atelier's session-local telemetry state."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _atelier_root() -> Path:
    return Path(os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT") or Path.home() / ".atelier")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        from atelier.core.capabilities.plugin_runtime import (
            build_session_progress_optimization_output,
            update_session_stats,
        )

        update_session_stats(_atelier_root(), payload)
        output = build_session_progress_optimization_output(_atelier_root(), payload)
        if output and not output.get("no_output"):
            print(json.dumps(output))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
