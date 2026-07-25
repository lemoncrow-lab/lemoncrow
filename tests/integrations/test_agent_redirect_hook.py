"""Tests for the Task-tool agent-redirect PreToolUse hook.

Mirrors tests/integrations/test_required_arg_nudge_hook.py: the hook is a
standalone script reading a JSON payload on stdin and printing an optional
JSON deny decision on stdout, exercised as a subprocess with crafted
payloads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / "integrations" / "claude" / "plugin" / "hooks"
HOOK = HOOKS / "agent_redirect.py"


def _run(
    payload: dict, tmp_path: Path, env_extra: dict | None = None, stdin_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "CLAUDE_WORKSPACE_ROOT": str(tmp_path),
        **(env_extra or {}),
    }
    stdin = stdin_text if stdin_text is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _task_payload(subagent_type: str) -> dict:
    return {"tool_name": "Task", "tool_input": {"subagent_type": subagent_type, "prompt": "do stuff"}}


def test_denies_general_purpose_and_redirects_to_lemoncrow_general(tmp_path: Path) -> None:
    proc = _run(_task_payload("general-purpose"), tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    hook_out = out["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    assert "lemoncrow:general" in hook_out["permissionDecisionReason"]


def test_denies_explore_and_redirects_to_lemoncrow_explore(tmp_path: Path) -> None:
    proc = _run(_task_payload("Explore"), tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    hook_out = out["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    assert "lemoncrow:explore" in hook_out["permissionDecisionReason"]


def test_stays_silent_for_already_namespaced_agent(tmp_path: Path) -> None:
    proc = _run(_task_payload("lemoncrow:general"), tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_stays_silent_for_unrelated_tool(tmp_path: Path) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_opt_out_env_var_disables_guard(tmp_path: Path) -> None:
    proc = _run(_task_payload("general-purpose"), tmp_path, env_extra={"LEMONCROW_AGENT_REDIRECT_GUARD": "0"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_malformed_stdin_exits_zero_with_no_output(tmp_path: Path) -> None:
    proc = _run({}, tmp_path, stdin_text="not json at all {{{")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_malformed_tool_input_is_ignored(tmp_path: Path) -> None:
    payload = {"tool_name": "Task", "tool_input": "not-a-dict"}
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
