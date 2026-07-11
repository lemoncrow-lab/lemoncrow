#!/usr/bin/env python3
"""Codex PreCompact/PostCompact hook backed by LemonCrow runtime state.

Records compaction lifecycle notes into the run ledger, snapshots pre-compaction
occupancy, and bumps the compaction epoch so the MCP server's content dedup
resets. Silent (no model-facing output). Fail-open: any error exits 0.
"""

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
        from lemoncrow.core.capabilities.plugin_runtime import (
            build_codex_post_compact_output,
            build_codex_pre_compact_output,
        )

        payload = json.loads(sys.stdin.read() or "{}")
        event = str(payload.get("hook_event_name") or "")
        root = _lemoncrow_root()
        if event == "PreCompact":
            build_codex_pre_compact_output(root, payload)
        elif event == "PostCompact":
            build_codex_post_compact_output(root, payload)
    except (ImportError, json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
