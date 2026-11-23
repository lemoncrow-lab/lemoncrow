#!/usr/bin/env python3
"""Capture native Write preimages and discard them after failed writes.

Silent, fail-open, and one-use. No preimage means PostToolUse must not claim
that the cumulative git diff is this specific Write's delta.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _root() -> Path:
    configured = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    if configured:
        return Path(configured)
    workspace = Path(os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd())
    state_path = workspace / ".lemoncrow" / "workspace" / "session_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(state, dict) and state.get("lemoncrow_root"):
            return Path(state["lemoncrow_root"])
    except (OSError, ValueError, TypeError):
        pass
    return Path.home() / ".lemoncrow"


def main() -> int:
    try:
        from lemoncrow.core.foundation.write_preimage import discard_write_preimage, stage_write_preimage

        payload = json.loads(sys.stdin.read() or "{}")
        if payload.get("tool_name") != "Write":
            return 0
        session_id = str(payload.get("session_id") or "").strip()
        tool_id = str(payload.get("tool_use_id") or "").strip()
        if payload.get("hook_event_name") == "PostToolUseFailure":
            discard_write_preimage(_root(), session_id, tool_id)
            return 0
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return 0
        file_path = str(tool_input.get("file_path") or tool_input.get("path") or "").strip()
        if not (session_id and tool_id and file_path):
            return 0
        target = Path(file_path).expanduser()
        if not target.is_absolute():
            target = Path(payload.get("cwd") or os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd()) / target
        stage_write_preimage(_root(), session_id, tool_id, target)
    except Exception:
        pass  # Fail-open: a snapshot must never deny a Write
    return 0


if __name__ == "__main__":
    sys.exit(main())
