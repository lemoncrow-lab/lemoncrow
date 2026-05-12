#!/usr/bin/env python3
"""PreToolUse hook that nudges file and SQL shell work toward Atelier tools.

Only active when ATELIER_DEV_MODE=1. In passive mode this hook exits silently
so the LLM is never told to use tools that aren't available.
"""

from __future__ import annotations

import json
import os
import sys


def _is_dev_mode() -> bool:
    return os.environ.get("ATELIER_DEV_MODE", "").lower() in ("1", "true", "yes")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool_name = payload.get("tool_name", "") or ""
        tool_input = payload.get("tool_input", {}) or {}

        if tool_name == "Bash":
            # Only redirect toward Atelier tools when dev mode is on.
            # In passive mode the tools don't exist — never suggest them.
            if not _is_dev_mode():
                return 0
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

        if tool_name == "Agent":
            # Agent subtype rewriting is mode-independent (just normalises names).
            from atelier.core.capabilities.plugin_runtime import rewrite_agent

            result = rewrite_agent(
                tool_input.get("subagent_type"),
                is_free_plan=os.environ.get("ATELIER_FREE_PLAN") == "1",
            )
            if result.get("updated_input"):
                updated = dict(tool_input)
                updated.update(result["updated_input"])
                print(
                    json.dumps(
                        {
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "allow",
                                "updatedInput": updated,
                            }
                        }
                    )
                )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
