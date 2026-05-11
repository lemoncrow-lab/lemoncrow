#!/usr/bin/env python3
"""PreToolUse hook for Edit/Write/MultiEdit.

Reads the hook payload from stdin. If the target file matches a risky path
or the recent context lacks a successful `lint` call, returns
a JSON decision telling Claude to call `lint` first.

This hook is **opt-in**. Enable it via hooks.json once the skills flow is
comfortable. It defaults to non-blocking (decision: "ask") to avoid
surprising users.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

RISKY_PATTERNS = [
    re.compile(p)
    for p in (
        r"(^|/)shopify(/|$)",
        r"(^|/)pdp(/|$)",
        r"(^|/)catalog(/|$)",
        r"(^|/)tracker(/|$)",
        r"(^|/)publish(/|$)",
        r"(^|/)schema(/|$)",
        r"alembic/versions/",
    )
]

PLAN_CHECK_TTL_SECONDS = 15 * 60  # a fresh plan check is good for 15 minutes


def _is_risky(path: str) -> bool:
    return any(p.search(path) for p in RISKY_PATTERNS)


def _state_path() -> Path:
    import hashlib

    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    h = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:12]
    root = Path(os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT") or Path.home() / ".atelier")
    return root / "workspaces" / h / "session_state.json"


def _recent_plan_check_ok() -> bool:
    sp = _state_path()
    if not sp.exists():
        return False
    try:
        state = json.loads(sp.read_text("utf-8"))
    except Exception:
        return False
    last = state.get("last_plan_check_ok_ts")
    if not isinstance(last, (int, float)):
        return False
    return (time.time() - last) < PLAN_CHECK_TTL_SECONDS


def _bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name, "").lower()
    if not val:
        return default
    return val in ("1", "true", "yes")


def _is_dev_mode() -> bool:
    return _bool_env("ATELIER_DEV_MODE", False)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0  # fail-open: never break the agent on hook parse error

    if not _is_dev_mode():
        print(json.dumps({"decision": "allow"}))
        return 0

    tool_input = payload.get("tool_input", {}) or {}
    target = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("filename") or ""
    if not target or not _is_risky(target):
        print(json.dumps({"decision": "allow"}))
        return 0

    if _recent_plan_check_ok():
        print(json.dumps({"decision": "allow"}))
        return 0

    msg = (
        f"Atelier: `{target}` is in a risky domain (shopify / pdp / catalog / "
        "tracker / publish / schema). Call `lint` with your "
        "current task and plan before editing."
    )
    print(json.dumps({"decision": "ask", "reason": msg}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
