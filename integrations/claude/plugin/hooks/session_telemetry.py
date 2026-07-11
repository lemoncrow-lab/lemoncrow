#!/usr/bin/env python3
"""Lifecycle hook that maintains LemonCrow's session-local telemetry state."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_THROTTLE_SECONDS = 1.0  # skip disk write if stats.json was written this recently


def _lemoncrow_root() -> Path:
    return Path(os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT") or Path.home() / ".lemoncrow")


def _throttled(root: Path, session_id: str) -> bool:
    """Return True if this call should skip disk I/O — stats.json was written recently."""
    from lemoncrow.core.capabilities.plugin_runtime import session_stats_path

    path = session_stats_path(root, session_id)
    if not path.exists():
        return False
    try:
        return (time.time() - path.stat().st_mtime) < _THROTTLE_SECONDS
    except OSError:
        return False


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        session_id = str(payload.get("session_id") or "default")
        root = _lemoncrow_root()

        if _throttled(root, session_id):
            # Recent write — skip disk I/O to reduce CPU/IO chatter.
            return 0

        from lemoncrow.core.capabilities.plugin_runtime import update_session_stats

        # Pure state upkeep — this hook emits nothing. Model-facing nudges are
        # limited to the user_prompt batching nudge and the failure-hook rescue
        # nudge; everything heuristic was removed as unproven noise.
        update_session_stats(root, payload)
    except Exception:  # noqa: BLE001 - lifecycle hooks must be fail-open
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
