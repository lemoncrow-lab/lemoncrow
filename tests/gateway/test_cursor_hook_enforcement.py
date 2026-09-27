"""Cursor deny-hooks: inert by default; soft cooloff; hard stick-deny.

Hard mode (LEMONCROW_CURSOR_ENFORCE=hard) is the codebench path: unscoped
preToolUse denies every non-LemonCrow MCP tool with no cooloff allow-through.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / "integrations" / "cursor" / "hooks"

DENY_HOOKS = [
    ("before_shell_execution.py", {"command": "ls"}),
    ("before_read_file.py", {"file_path": "a.py"}),
    ("before_tool_use.py", {"tool_name": "Grep"}),
]


def _run_hook(script: str, payload: dict, root: Path, env: dict[str, str] | None = None) -> dict:
    proc = subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "LEMONCROW_ROOT": str(root), **(env or {})},
    )
    assert proc.returncode == 0, f"{script} exited {proc.returncode}: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("script,payload", DENY_HOOKS, ids=[s for s, _ in DENY_HOOKS])
def test_hook_allows_by_default(script: str, payload: dict, tmp_path: Path) -> None:
    out = _run_hook(script, payload, tmp_path, env={"LEMONCROW_CURSOR_ENFORCE": ""})
    assert out["permission"] == "allow"


@pytest.mark.parametrize("script,payload", DENY_HOOKS, ids=[s for s, _ in DENY_HOOKS])
def test_hook_denies_when_enforcement_opted_in(script: str, payload: dict, tmp_path: Path) -> None:
    out = _run_hook(script, payload, tmp_path, env={"LEMONCROW_CURSOR_ENFORCE": "1"})
    assert out["permission"] == "deny"
    assert "LemonCrow" in out["user_message"]


@pytest.mark.parametrize("script,payload", DENY_HOOKS, ids=[s for s, _ in DENY_HOOKS])
def test_default_allow_does_not_write_deny_state(script: str, payload: dict, tmp_path: Path) -> None:
    _run_hook(script, payload, tmp_path, env={"LEMONCROW_CURSOR_ENFORCE": ""})
    assert not list((tmp_path / "cursor-hooks").glob("*_deny_state.json"))


@pytest.mark.parametrize(
    "tool_name",
    ["Glob", "Shell", "Read", "Grep", "Write", "StrReplace", "Delete", "Task", "WebSearch"],
)
def test_hard_denies_natives_without_cooloff(tool_name: str, tmp_path: Path) -> None:
    env = {"LEMONCROW_CURSOR_ENFORCE": "hard"}
    first = _run_hook("before_tool_use.py", {"tool_name": tool_name}, tmp_path, env=env)
    assert first["permission"] == "deny"
    # Immediate retry must still deny (no cooloff allow-through).
    second = _run_hook("before_tool_use.py", {"tool_name": tool_name}, tmp_path, env=env)
    assert second["permission"] == "deny"


@pytest.mark.parametrize(
    "tool_name",
    [
        "MCP:code_search",
        "MCP:read",
        "MCP:edit",
        "MCP:bash",
        "MCP:web_fetch",
        "MCP:lemoncrow:code_search",
        "mcp__lemoncrow__read",
    ],
)
def test_hard_allows_lemoncrow_mcp_tools(tool_name: str, tmp_path: Path) -> None:
    out = _run_hook(
        "before_tool_use.py",
        {"tool_name": tool_name},
        tmp_path,
        env={"LEMONCROW_CURSOR_ENFORCE": "hard"},
    )
    assert out["permission"] == "allow"


def test_hard_denies_get_mcp_tools_meta(tmp_path: Path) -> None:
    out = _run_hook(
        "before_tool_use.py",
        {"tool_name": "GetMcpTools"},
        tmp_path,
        env={"LEMONCROW_CURSOR_ENFORCE": "hard"},
    )
    assert out["permission"] == "deny"


def test_hard_shell_and_read_stick_deny(tmp_path: Path) -> None:
    env = {"LEMONCROW_CURSOR_ENFORCE": "hard"}
    for script, payload in [
        ("before_shell_execution.py", {"command": "ls"}),
        ("before_read_file.py", {"file_path": "a.py"}),
    ]:
        a = _run_hook(script, payload, tmp_path, env=env)
        b = _run_hook(script, payload, tmp_path, env=env)
        assert a["permission"] == "deny"
        assert b["permission"] == "deny"


def test_soft_allows_immediate_retry(tmp_path: Path) -> None:
    env = {"LEMONCROW_CURSOR_ENFORCE": "1"}
    first = _run_hook("before_tool_use.py", {"tool_name": "Grep"}, tmp_path, env=env)
    assert first["permission"] == "deny"
    second = _run_hook("before_tool_use.py", {"tool_name": "Grep"}, tmp_path, env=env)
    assert second["permission"] == "allow"
