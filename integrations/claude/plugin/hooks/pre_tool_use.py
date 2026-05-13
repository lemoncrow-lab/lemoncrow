#!/usr/bin/env python3
"""PreToolUse hook for Edit/Write/MultiEdit.

Reads the hook payload from stdin. If the target file matches a risky path,
returns a JSON decision telling Claude to call `task` first.

This hook is **opt-in**. Enable it via hooks.json once the skills flow is
comfortable. It defaults to non-blocking (decision: "ask") to avoid
surprising users.
"""

from __future__ import annotations

import json
import re
import sys

from atelier.core.environment import is_dev_mode

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


def _is_risky(path: str) -> bool:
    return any(p.search(path) for p in RISKY_PATTERNS)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0  # fail-open: never break the agent on hook parse error

    if not is_dev_mode():
        print(json.dumps({"decision": "allow"}))
        return 0

    tool_input = payload.get("tool_input", {}) or {}
    target = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("filename") or ""
    if not target or not _is_risky(target):
        print(json.dumps({"decision": "allow"}))
        return 0

    msg = (
        f"Atelier: `{target}` is in a risky domain (shopify / pdp / catalog / "
        "tracker / publish / schema). Call `task` with your "
        "current goal before editing."
    )
    print(json.dumps({"decision": "ask", "reason": msg}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
