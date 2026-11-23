#!/usr/bin/env python3
"""Claude's /code-review of the pull request it just opened lands in lc review and on that pull request.

PostToolUse(ReportFindings) carries a foreground review's findings; PostToolUse(Skill)
carries a forked code-review the Skill tool waited for, as the fork's final JSON; SubagentStop
carries a background one's (only where Claude Code marks the fork with a sidecar).
Only a report that answers a pull request this session just opened (see review_pr_opened.py)
is saved: the hook names that pull request's Review and hands the report to a detached worker,
which posts it on the pull request as one numbered review, captures the pull request's range
and has the server import it. A code review of no pull request is left as Claude reported it.

The user sees one plain line (and, for a forked review, the numbered list); after a
foreground review Claude gets the numbering and is told to fix every finding it agrees with
now, recording each with `lc review finding N fixed --run`, and to ask the user only before
skipping one (a background review's SubagentStop cannot add context to the conversation
that started it). Fail-open: a hosted configuration or a non-git directory prints nothing,
and the hook never blocks or waits for the capture.
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
    # Every subagent stop and every Skill call reaches this hook; anything but a code review returns early.
    forked = payload.get("hook_event_name") == "SubagentStop"
    tool_name = payload.get("tool_name")
    skill_input = payload.get("tool_input") if tool_name == "Skill" else None
    skill = isinstance(skill_input, dict) and str(skill_input.get("skill") or "").split(":")[-1] == "code-review"
    if not forked and not skill and tool_name != "ReportFindings":
        return 0
    payload.setdefault("cwd", os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd())
    try:
        from lemoncrow.core.capabilities.session_comment_capture import reported_findings_rows
        from lemoncrow.pro.capabilities.review import pr_trigger
        from lemoncrow.pro.capabilities.review import reported_findings as reported

        if forked:
            report = reported.forked_review_payload(payload)
        else:
            report = reported.forked_skill_payload(payload) if skill else payload
        tool_input = report.get("tool_input") if report else None
        findings = tool_input.get("findings") if isinstance(tool_input, dict) else None
        if report is None or not isinstance(findings, list) or _dormant():
            return 0
        root = _lemoncrow_root()
        if reported.already_reported(report, host="claude", store_root=root):
            return 0
        pull = pr_trigger.claim_pull_request(
            root, host="claude", session_id=str(report.get("session_id") or ""), cwd=Path(str(report["cwd"]))
        )
        if pull is None:
            # lc review saves and numbers a pull request's code review only.
            return 0
        report = {**report, "pull_request": pull.to_wire()}
        # Fire and forget: one quick read names the Review; a detached worker posts, captures and imports.
        url = reported.pull_request_review_url(report, store_root=root)
        if url is None:
            return 0
        reported.note_report(report, host="claude", store_root=root, url=url)
        reported.spawn_reported_findings(report, host="claude", store_root=root)
        rows = reported_findings_rows(findings)
        # Claude was told to fix them: by the context below, or, for a forked review, by the pull request's own
        # instruction.
        message = reported.report_message(rows, url, pr_number=pull.number, listed=forked or skill)
        context = reported.report_context(rows, url, pull_request=pull)
    except Exception:
        return 0
    output: dict[str, object] = {"systemMessage": message}
    if not forked:
        # Not on SubagentStop: additionalContext there keeps the forked reviewer running instead of reaching
        # this conversation. The PR trigger's instruction already names the commands for that case.
        output["hookSpecificOutput"] = {"hookEventName": "PostToolUse", "additionalContext": context}
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
