#!/usr/bin/env python3
"""Claude just opened a pull request: have it review that pull request now, in the chat.

PostToolUse(Bash | GitHub MCP create_pull_request). Only a successful creation that
printed the new pull request's URL counts (`gh pr create` that failed exits non-zero,
so it never reaches PostToolUse with a URL on standard output). Each head commit
triggers once. The hook notes the pull request for the review that follows, so
review_findings.py saves those findings on the pull request's own Review, and tells
Claude (additionalContext) to run its code-review skill on it with --comment.

Fast and fail-open: no network, a few git reads. Silent under LEMONCROW_IN_REVIEW,
when dormant, outside a checkout, or with a hosted Review configuration.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _session_state_path() -> Path:
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    return Path(workspace).expanduser().resolve() / ".lemoncrow" / "workspace" / "session_state.json"


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    if root:
        return Path(root)
    try:
        state = json.loads(_session_state_path().read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        state = {}
    if isinstance(state, dict) and state.get("lemoncrow_root"):
        return Path(state["lemoncrow_root"])
    return Path.home() / ".lemoncrow"


def _dormant() -> bool:
    try:
        from lemoncrow.core.capabilities.plugin_runtime import cap_exhausted

        return bool(cap_exhausted(_lemoncrow_root()))
    except Exception:
        return False


def main() -> int:
    if os.environ.get("LEMONCROW_IN_REVIEW"):
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        from lemoncrow.pro.capabilities.review import pr_trigger

        opened = pr_trigger.opened_pull_request(payload)
        if opened is None or _dormant():
            return 0
        cwd = Path(str(payload.get("cwd") or os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd()))
        if not pr_trigger.local_review_configured(cwd):
            return 0
        pending = pr_trigger.record_pr_trigger(
            _lemoncrow_root(), opened, host="claude", session_id=str(payload.get("session_id") or ""), cwd=cwd
        )
        if pending is None:
            return 0
        context = pr_trigger.review_instruction(pending)
    except Exception:
        return 0
    message = f"lc review: reviewing PR #{pending.number}…"
    print(
        json.dumps(
            {
                "systemMessage": message,
                "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context},
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
