#!/usr/bin/env python3
"""Claude's /code-review lands in lc review. Fire and forget.

PostToolUse(ReportFindings) carries a foreground review's findings; SubagentStop
carries a background review's, as the forked code-review skill's final JSON.
The hook names the branch Review's URL and hands the report to a detached
worker, which captures the change, binds the findings to that Snapshot and has
the server import them. Every report prints its URL, a clean one included.
Fail-open: a hosted configuration or a non-git directory prints nothing, and
the hook never blocks or waits for the capture.
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
    # Every subagent stop reaches this hook; anything but a code-review fork returns early.
    forked = payload.get("hook_event_name") == "SubagentStop"
    if not forked and payload.get("tool_name") != "ReportFindings":
        return 0
    payload.setdefault("cwd", os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd())
    try:
        from lemoncrow.pro.capabilities.review import reported_findings as reported

        report = reported.forked_review_payload(payload) if forked else payload
        tool_input = report.get("tool_input") if report else None
        findings = tool_input.get("findings") if isinstance(tool_input, dict) else None
        if report is None or not isinstance(findings, list) or _dormant():
            return 0
        root = _lemoncrow_root()
        # Fire and forget: two quick reads name the Review; a detached worker captures and imports.
        url = reported.branch_review_url(report, store_root=root)
        if url is None:
            return 0
        reported.spawn_reported_findings(report, host="claude", store_root=root)
    except Exception:
        return 0
    print(json.dumps({"systemMessage": reported.report_message("claude", len(findings), url)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
