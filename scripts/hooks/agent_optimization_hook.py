#!/usr/bin/env python3
import json
import sys


def main():
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            sys.exit(0)

        payload = json.loads(raw_input)
        tool_name = payload.get("tool_name", "")

        # Only process PreToolUse for Agent
        if tool_name != "Agent":
            sys.exit(0)

        # We inject strict instructions for the subagent so it uses our cost-optimized tools
        nudge_msg = """
[SYSTEM CONSTRAINT FOR SUBAGENT]
You are running as a subagent in an optimized Atelier environment.
You MUST follow these strict rules to save tokens and avoid loops:
1. NEVER use the native Bash tool for reading files (e.g. cat, grep).
2. ALWAYS use `mcp__atelier__search` for codebase exploration.
3. ALWAYS use `mcp__atelier__edit` for modifying code.
4. Batch your edits into a single `mcp__atelier__edit` call whenever possible.
Failure to follow these constraints will result in immediate termination.
"""

        # Return the exact JSON envelope Claude expects for PreToolUse
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": nudge_msg.strip(),
            }
        }
        print(json.dumps(output))

    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
