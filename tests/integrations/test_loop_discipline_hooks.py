"""Tests for the edit-tracking + read-after-edit guard plugin hooks.

The hooks are standalone scripts that read a JSON payload on stdin and print a
JSON decision on stdout, so we exercise them as subprocesses with crafted
payloads -- isolating session state under a per-test ATELIER_ROOT.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / "integrations" / "claude" / "plugin" / "hooks"


def _run(hook: str, payload: dict, tmp_path: Path, env_extra: dict | None = None) -> str:
    env = {
        **os.environ,
        "CLAUDE_WORKSPACE_ROOT": str(tmp_path),
        "ATELIER_ROOT": str(tmp_path / ".atelier"),
        **(env_extra or {}),
    }
    proc = subprocess.run(
        [sys.executable, str(HOOKS / hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_edit_tracking_then_read_after_edit_blocks_expand_reread(tmp_path: Path) -> None:
    # loop_discipline_post records the edit (no output), then pre_tool_discipline
    # blocks a full expand re-read of that same file.
    edit = {
        "tool_name": "mcp__atelier__edit",
        "tool_input": {"edits": [{"file_path": "shop/pricing.py", "old_string": "a", "new_string": "b"}]},
    }
    assert _run("loop_discipline_post.py", edit, tmp_path) == ""

    expand_reread = {"tool_name": "mcp__atelier__read", "tool_input": {"path": "shop/pricing.py", "full": True}}
    out = _run("pre_tool_discipline.py", expand_reread, tmp_path)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"

    # a targeted range read of the same file is allowed
    range_read = {"tool_name": "mcp__atelier__read", "tool_input": {"path": "shop/pricing.py", "range": "L1-L20"}}
    assert _run("pre_tool_discipline.py", range_read, tmp_path) == ""

    # an expand read of a file NOT edited this session is allowed
    other = {"tool_name": "mcp__atelier__read", "tool_input": {"path": "shop/other.py", "full": True}}
    assert _run("pre_tool_discipline.py", other, tmp_path) == ""

    # opt-out via env
    assert _run("pre_tool_discipline.py", expand_reread, tmp_path, {"ATELIER_READ_AFTER_EDIT_GUARD": "0"}) == ""


def test_read_after_edit_no_block_without_prior_edit(tmp_path: Path) -> None:
    expand_reread = {"tool_name": "mcp__atelier__read", "tool_input": {"path": "shop/pricing.py", "full": True}}
    assert _run("pre_tool_discipline.py", expand_reread, tmp_path) == ""


def test_workspace_code_grep_is_not_blocked(tmp_path: Path) -> None:
    # The grep->explore hard block was removed: steering toward explore lives in
    # agent disallowedTools + tool descriptions, not a PreToolUse deny (which
    # mis-fired on legitimate searches such as greps over other repos).
    payload = {"tool_name": "mcp__atelier__bash", "tool_input": {"command": "grep -rn handleAuth src/"}}
    assert _run("pre_tool_discipline.py", payload, tmp_path) == ""


def test_other_repo_grep_is_not_blocked(tmp_path: Path) -> None:
    payload = {
        "tool_name": "mcp__atelier__bash",
        "tool_input": {"command": "cd /srv/other-repo && grep -rn handleAuth ."},
    }
    assert _run("pre_tool_discipline.py", payload, tmp_path) == ""


def test_abs_path_grep_is_not_blocked(tmp_path: Path) -> None:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "grep -rn handleAuth /srv/other-repo/src"},
    }
    assert _run("pre_tool_discipline.py", payload, tmp_path) == ""


def test_pipe_grep_is_not_blocked(tmp_path: Path) -> None:
    # A non-leading grep (pipe filter) was never a code search anyway.
    payload = {"tool_name": "Bash", "tool_input": {"command": "ps aux | grep python"}}
    assert _run("pre_tool_discipline.py", payload, tmp_path) == ""
