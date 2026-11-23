#!/usr/bin/env python3
"""Codex just opened a pull request: have it review that pull request now, in the chat.

Codex PostToolUse on a shell call (or a GitHub MCP create_pull_request). Only a
successful `gh pr create` that printed the new pull request's URL counts; each head
commit triggers once. Codex has no code-review skill and no ReportFindings tool, so
the additionalContext asks Codex to review the pull request's diff, post the
findings on it with `gh api`, and hand the same findings to
`lemoncrow review report-findings`, which saves them on the pull request's Review and
prints the numbered list for the chat.

Fast and fail-open: no network, a few git reads. Silent under LEMONCROW_IN_REVIEW,
when dormant, outside a checkout, or with a hosted Review configuration.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    if root:
        return Path(root)
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
        cwd = Path(str(payload.get("cwd") or os.getcwd()))
        if not pr_trigger.local_review_configured(cwd):
            return 0
        pending = pr_trigger.record_pr_trigger(
            _lemoncrow_root(), opened, host="codex", session_id=str(payload.get("session_id") or ""), cwd=cwd
        )
        if pending is None:
            return 0
        context = pr_trigger.codex_review_instruction(pending)
    except Exception:
        return 0
    message = f"lc review: reviewing PR #{pending.number} now; findings go on the PR and are listed here"
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
