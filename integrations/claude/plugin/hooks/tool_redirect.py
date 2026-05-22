#!/usr/bin/env python3
"""PreToolUse hook that nudges file and SQL shell work toward Atelier tools.

Only active when ATELIER_DEV_MODE=1. In stable mode this hook exits silently
so the LLM is never told to use tools that aren't available.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _bootstrap_atelier_path() -> None:
    """Make hooks runnable from a copied Claude plugin without PYTHONPATH."""
    here = Path(__file__).resolve()
    candidates = [
        here.parents[4] / "src",  # repo or ~/.local/share/atelier install layout
        Path.home() / ".local" / "share" / "atelier" / "src",
    ]
    for candidate in candidates:
        if candidate.exists():
            path = str(candidate)
            if path not in sys.path:
                sys.path.insert(0, path)


def _is_dev_mode() -> bool:
    try:
        _bootstrap_atelier_path()
        from atelier.core.environment import is_dev_mode

        return is_dev_mode()
    except Exception:
        return False


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool_name = payload.get("tool_name", "") or ""
        tool_input = payload.get("tool_input", {}) or {}

        if tool_name == "Bash":
            # Only redirect toward Atelier tools when dev mode is on.
            # In stable mode the dev tools don't exist — never suggest them.
            if not _is_dev_mode():
                return 0
            _bootstrap_atelier_path()
            from atelier.core.capabilities.plugin_runtime import classify_bash

            result = classify_bash(str(tool_input.get("command", "") or ""))
            if result.get("no_output"):
                return 0
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                            "additionalContext": result.get("additional_context", ""),
                        }
                    }
                )
            )
            return 0

        # Agent subtype rewriting removed. The host's namespaced resolver
        # handles `atelier:*` correctly on its own; our previous rewrite
        # stripped the namespace and broke `atelier:explore`.
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
