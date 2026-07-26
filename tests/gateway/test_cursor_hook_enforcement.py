"""Cursor deny-hooks are inert unless explicitly enabled.

Measured on Cursor with a multi-turn edit task (3 runs per arm, billed-token
accounting): denying the native Read/Shell/Grep tools bought no routing onto the
lc MCP tools -- the loop guard lets the immediate retry through, so the native
call ran anyway -- while the denied turns pushed the agent onto
``glob_file_search **/*``, the one orientation tool Cursor exposes no hook
matcher for. That returned ~38K characters of ``.venv``/``.pytest_cache``
listing in 2 of 3 runs, which every later turn re-pays for as cache_read. The
run with no denies landed at/below the vanilla baseline; the runs with denies
cost ~1.8x it.

So the hooks install but stay inert; ``LEMONCROW_CURSOR_ENFORCE=1`` restores the
old deny-and-redirect behavior for anyone who wants a hard nudge.
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

# (script, payload) pairs -- one per deny-capable hook.
DENY_HOOKS = [
    ("before_shell_execution.py", {"command": "ls"}),
    ("before_read_file.py", {"file_path": "a.py"}),
    ("before_tool_use.py", {"tool_name": "Grep"}),
]


def _run_hook(script: str, payload: dict, root: Path, env: dict[str, str] | None = None) -> dict:
    """Invoke a hook the way Cursor does: JSON on stdin, one JSON object on stdout.

    ``LEMONCROW_ROOT`` is pinned to a tmp dir so each case gets a fresh
    deny-state file and never inherits the developer's real cooloff window.
    """
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
    """Fresh state + no opt-in -> allow, so the native tool is never blocked."""
    out = _run_hook(script, payload, tmp_path, env={"LEMONCROW_CURSOR_ENFORCE": ""})
    assert out["permission"] == "allow"


@pytest.mark.parametrize("script,payload", DENY_HOOKS, ids=[s for s, _ in DENY_HOOKS])
def test_hook_denies_when_enforcement_opted_in(script: str, payload: dict, tmp_path: Path) -> None:
    """Opt-in restores the deny-and-redirect nudge on a first (uncooled) attempt."""
    out = _run_hook(script, payload, tmp_path, env={"LEMONCROW_CURSOR_ENFORCE": "1"})
    assert out["permission"] == "deny"
    assert "LemonCrow" in out["user_message"]


@pytest.mark.parametrize("script,payload", DENY_HOOKS, ids=[s for s, _ in DENY_HOOKS])
def test_default_allow_does_not_write_deny_state(script: str, payload: dict, tmp_path: Path) -> None:
    """Inert hooks stay side-effect free -- no cooloff state to leak into a later
    opt-in session, and no per-call disk write on the hot path."""
    _run_hook(script, payload, tmp_path, env={"LEMONCROW_CURSOR_ENFORCE": ""})
    assert not list((tmp_path / "cursor-hooks").glob("*_deny_state.json"))
