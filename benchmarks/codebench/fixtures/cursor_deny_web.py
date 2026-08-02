#!/usr/bin/env python3
"""Cursor hook: deny WebSearch/WebFetch so baseline cannot fetch gold PRs."""

from __future__ import annotations

import json
import sys

_DENIED = {
    "websearch",
    "webfetch",
    "web_search",
    "web_fetch",
    "mcp:web_fetch",
    "mcp:web_search",
}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    name = str(payload.get("tool_name") or "").strip().lower()
    if name in _DENIED or name.endswith(":web_fetch") or name.endswith(":web_search"):
        sys.stdout.write(
            json.dumps(
                {
                    "permission": "deny",
                    "agent_message": (
                        f"'{payload.get('tool_name')}' is disabled for this hermetic benchmark. "
                        "Solve from the local repository only."
                    ),
                }
            )
            + "\n"
        )
        return 0
    sys.stdout.write(json.dumps({"permission": "allow"}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
