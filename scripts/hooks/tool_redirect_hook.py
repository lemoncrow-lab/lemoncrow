#!/usr/bin/env python3
import json
import re
import sys


def main():
    try:
        # Claude Code platform hooks read exactly one JSON object from stdin
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            sys.exit(0)

        payload = json.loads(raw_input)
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input", {})

        # Only process PreToolUse for Bash
        if tool_name != "Bash" and tool_name != "mcp__atelier__Bash":
            sys.exit(0)

        command = tool_input.get("command", "")
        if not command:
            sys.exit(0)

        # Strip subshells and strings to look at the base command safely
        clean_cmd = re.sub(r"\$\(.*?\)|`.*?`", "", command)

        # Split by pipes and logical operators to check the first utility of each segment
        segments = re.split(r"&&|\|\||;|\|", clean_cmd)

        read_tools = {"cat", "grep", "rg", "sed", "find", "ls", "awk", "head", "tail", "less", "more"}
        write_tools = {"sed", "echo"}  # Very basic heuristics, `sed -i` or `echo >`
        sql_tools = {"psql", "sqlite3", "mysql", "pg_dump", "pg_restore"}

        nudge_msg = None

        for segment in segments:
            segment = segment.strip()
            if not segment:
                continue

            parts = segment.split()
            if not parts:
                continue

            utility = parts[0]

            # Simple allowlist for common safe build/test commands
            if utility in {
                "git",
                "docker",
                "python",
                "python3",
                "npm",
                "yarn",
                "make",
                "pytest",
                "curl",
                "wget",
                "node",
                "uv",
            }:
                continue

            if utility in sql_tools:
                nudge_msg = "Use the mcp__atelier__sql tool for database queries, not Bash."
                break

            if utility in write_tools and (">" in segment or "-i" in segment):
                nudge_msg = "Use the mcp__atelier__edit tool for writing and editing files, not Bash."
                break

            if utility in read_tools:
                nudge_msg = "Use the mcp__atelier__search tool for reading files and searching code, not Bash."
                break

        if nudge_msg:
            # We must output the exact JSON envelope Claude expects for PreToolUse
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "additionalContext": f"IMPORTANT: {nudge_msg} This command was allowed to run, but you MUST use the correct tool for all subsequent calls. Reminder: use mcp__atelier__search for discovering and loading files and mcp__atelier__edit for editing files.",
                }
            }
            print(json.dumps(output))

    except Exception:
        # Always exit 0 so we don't break the Claude execution loop on a parsing error
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
