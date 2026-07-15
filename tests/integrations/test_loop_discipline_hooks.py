"""Tests for the edit-tracking + read-after-edit guard plugin hooks.

The hooks are standalone scripts that read a JSON payload on stdin and print a
JSON decision on stdout, so we exercise them as subprocesses with crafted
payloads -- isolating session state under a per-test LEMONCROW_ROOT.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from tests.helpers import python_script_with_development_cap

HOOKS = Path(__file__).resolve().parents[2] / "integrations" / "claude" / "plugin" / "hooks"


def _run(hook: str, payload: dict, tmp_path: Path, env_extra: dict | None = None) -> str:
    env = {
        **os.environ,
        "CLAUDE_WORKSPACE_ROOT": str(tmp_path),
        "LEMONCROW_ROOT": str(tmp_path / ".lemoncrow"),
        **(env_extra or {}),
    }
    proc = subprocess.run(
        python_script_with_development_cap(HOOKS / hook),
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
        "tool_name": "mcp__lc__edit",
        "tool_input": {"edits": [{"file_path": "shop/pricing.py", "old_string": "a", "new_string": "b"}]},
    }
    assert _run("loop_discipline_post.py", edit, tmp_path) == ""

    expand_reread = {"tool_name": "mcp__lc__read", "tool_input": {"path": "shop/pricing.py", "full": True}}
    out = _run("pre_tool_discipline.py", expand_reread, tmp_path)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"

    # a targeted range read of the same file is allowed
    range_read = {"tool_name": "mcp__lc__read", "tool_input": {"path": "shop/pricing.py", "range": "L1-L20"}}
    assert _run("pre_tool_discipline.py", range_read, tmp_path) == ""

    # an expand read of a file NOT edited this session is allowed
    other = {"tool_name": "mcp__lc__read", "tool_input": {"path": "shop/other.py", "full": True}}
    assert _run("pre_tool_discipline.py", other, tmp_path) == ""

    # opt-out via env
    assert _run("pre_tool_discipline.py", expand_reread, tmp_path, {"LEMONCROW_READ_AFTER_EDIT_GUARD": "0"}) == ""


def test_files_schema_full_reads_blocked_after_edit(tmp_path: Path) -> None:
    # The read tool's real input shape is files=[]; the guard must catch full
    # reads expressed as ':full' strings, bare-path strings, and dict entries.
    edit = {
        "tool_name": "mcp__lc__edit",
        "tool_input": {"edits": [{"path": "shop/pricing.py:L3-L9", "new": "x"}]},
    }
    assert _run("loop_discipline_post.py", edit, tmp_path) == ""

    def denied(tool_input: dict) -> bool:
        out = _run("pre_tool_discipline.py", {"tool_name": "mcp__lc__read", "tool_input": tool_input}, tmp_path)
        return bool(out) and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"

    assert denied({"files": ["shop/pricing.py:full"]})
    assert denied({"files": ["shop/pricing.py"]})  # bare path = whole file
    assert denied({"files": [{"path": "shop/pricing.py", "full": True}]})
    assert denied({"files": [{"path": "shop/pricing.py"}]})
    # bounded reads pass
    assert not denied({"files": ["shop/pricing.py:L1-L20"]})
    assert not denied({"files": ["shop/pricing.py:summary"]})
    assert not denied({"files": [{"path": "shop/pricing.py", "range": "L1-L20"}]})
    # full read of an un-edited file passes
    assert not denied({"files": ["shop/other.py:full"]})


def test_non_read_tool_with_files_array_is_not_denied(tmp_path: Path) -> None:
    # A non-read tool (e.g. StructuredOutput) may carry a files=[...] array in
    # its payload. The guard must key off the tool NAME, not the presence of a
    # 'files' key -- otherwise an enumerated path that is in the edited set
    # produces a bogus :full-read deny that blocks the tool call entirely.
    edit = {
        "tool_name": "mcp__lc__edit",
        "tool_input": {"edits": [{"path": "shop/pricing.py:L3-L9", "new": "x"}]},
    }
    assert _run("loop_discipline_post.py", edit, tmp_path) == ""

    structured = {
        "tool_name": "StructuredOutput",
        "tool_input": {"summary": "scope", "files": ["shop/pricing.py", "shop/other.py"]},
    }
    assert _run("pre_tool_discipline.py", structured, tmp_path) == ""


def test_edit_by_one_agent_does_not_block_read_by_another(tmp_path: Path) -> None:
    # State is keyed per-agent (agent_id, else session_id, else "main"). An edit
    # by the top-level agent must NOT block a read-only sub-agent that only
    # enumerates/reads the same file -- the bug that stalled scope agents.
    edit = {
        "tool_name": "mcp__lc__edit",
        "tool_input": {"edits": [{"path": "shop/pricing.py:L3-L9", "new": "x"}]},
    }
    assert _run("loop_discipline_post.py", edit, tmp_path) == ""

    sub_read = {
        "tool_name": "mcp__lc__read",
        "agent_id": "a903e6286e60304c8",
        "tool_input": {"files": ["shop/pricing.py:full"]},
    }
    assert _run("pre_tool_discipline.py", sub_read, tmp_path) == ""

    # ...but the editing agent re-reading the whole file IS still denied.
    main_read = {"tool_name": "mcp__lc__read", "tool_input": {"files": ["shop/pricing.py:full"]}}
    out = _run("pre_tool_discipline.py", main_read, tmp_path)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_subagent_full_reread_of_its_own_edit_is_denied(tmp_path: Path) -> None:
    # Per-agent scoping must not weaken the guard within one agent: a sub-agent
    # that edits then :full-rereads the same file is still denied.
    aid = "a92a09810b278eaf7"
    edit = {
        "tool_name": "mcp__lc__edit",
        "agent_id": aid,
        "tool_input": {"edits": [{"path": "shop/pricing.py:L3-L9", "new": "x"}]},
    }
    assert _run("loop_discipline_post.py", edit, tmp_path) == ""
    reread = {
        "tool_name": "mcp__lc__read",
        "agent_id": aid,
        "tool_input": {"files": ["shop/pricing.py:full"]},
    }
    out = _run("pre_tool_discipline.py", reread, tmp_path)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_prune_removes_stale_agent_state_keeps_fresh(tmp_path: Path) -> None:
    # Ephemeral sub-agent files are pruned by mtime (>24h); active ones survive.
    edit = {
        "tool_name": "mcp__lc__edit",
        "agent_id": "fresh",
        "tool_input": {"edits": [{"path": "a.py:L1-L2", "new": "x"}]},
    }
    assert _run("loop_discipline_post.py", edit, tmp_path) == ""
    state_dirs = list((tmp_path / ".lemoncrow").glob("workspaces/*/loop_discipline"))
    assert state_dirs, "per-agent state dir created"
    sd = state_dirs[0]
    assert (sd / "fresh.json").exists()

    stale = sd / "stale.json"
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - 90_000  # >24h
    os.utime(stale, (old, old))

    # A later edit triggers _prune().
    edit2 = {
        "tool_name": "mcp__lc__edit",
        "agent_id": "fresh",
        "tool_input": {"edits": [{"path": "b.py:L1-L2", "new": "y"}]},
    }
    assert _run("loop_discipline_post.py", edit2, tmp_path) == ""
    assert not stale.exists(), "stale per-agent file pruned"
    assert (sd / "fresh.json").exists(), "active per-agent file kept"


def test_basename_collision_does_not_false_positive(tmp_path: Path) -> None:
    # utils.py edited in one package must not block a full read of a different
    # package's utils.py -- comparison is on resolved paths, not basenames.
    edit = {
        "tool_name": "mcp__lc__edit",
        "tool_input": {"edits": [{"file_path": "pkg_a/utils.py", "old_string": "a", "new_string": "b"}]},
    }
    assert _run("loop_discipline_post.py", edit, tmp_path) == ""

    other = {"tool_name": "mcp__lc__read", "tool_input": {"path": "pkg_b/utils.py", "full": True}}
    assert _run("pre_tool_discipline.py", other, tmp_path) == ""

    same = {"tool_name": "mcp__lc__read", "tool_input": {"path": "pkg_a/utils.py", "full": True}}
    out = _run("pre_tool_discipline.py", same, tmp_path)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_read_after_edit_no_block_without_prior_edit(tmp_path: Path) -> None:
    expand_reread = {"tool_name": "mcp__lc__read", "tool_input": {"path": "shop/pricing.py", "full": True}}
    assert _run("pre_tool_discipline.py", expand_reread, tmp_path) == ""


def test_workspace_code_grep_is_not_blocked(tmp_path: Path) -> None:
    # The grep->explore hard block was removed: steering toward explore lives in
    # agent disallowedTools + tool descriptions, not a PreToolUse deny (which
    # mis-fired on legitimate searches such as greps over other repos).
    payload = {"tool_name": "mcp__lc__bash", "tool_input": {"command": "grep -rn handleAuth src/"}}
    assert _run("pre_tool_discipline.py", payload, tmp_path) == ""


def test_other_repo_grep_is_not_blocked(tmp_path: Path) -> None:
    payload = {
        "tool_name": "mcp__lc__bash",
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
