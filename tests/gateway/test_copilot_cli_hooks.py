from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "integrations" / "copilot-cli" / "hooks"


def _run_failure(root: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ATELIER_ROOT"] = str(root)
    return subprocess.run(
        [sys.executable, str(HOOKS / "post_tool_use_failure.py")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_copilot_repeated_failure_injects_rescue_on_second_match(tmp_path: Path) -> None:
    payload = {
        "sessionId": "s1",
        "toolName": "bash",
        "toolArgs": {"command": "make test"},
        "error": "same failure",
    }

    first = _run_failure(tmp_path / ".atelier", payload)
    second = _run_failure(tmp_path / ".atelier", payload)

    assert first.returncode == 0
    assert first.stdout == ""
    assert second.returncode == 2
    assert "Call 'rescue' before any retry" in second.stdout


def test_copilot_different_failure_does_not_trigger_rescue(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    base = {
        "sessionId": "s1",
        "toolName": "bash",
        "toolArgs": {"command": "make test"},
    }

    first = _run_failure(root, {**base, "error": "failure one"})
    second = _run_failure(root, {**base, "error": "failure two"})

    assert first.returncode == 0
    assert second.returncode == 0
    assert second.stdout == ""


def test_copilot_session_start_writes_session_state_bridge(tmp_path: Path) -> None:
    """sessionStart must bridge the live session id into session_state.json.

    Copilot CLI doesn't set GITHUB_COPILOT_SESSION_ID for the MCP subprocess,
    so without this bridge every savings row is quarantined unattributed and
    the session shows Saved $0 (mcp_server._workspace_bridge_session_id reads
    this file as its fallback).
    """
    root = tmp_path / ".atelier"
    ws = tmp_path / "repo"
    ws.mkdir()
    env = os.environ.copy()
    env["ATELIER_ROOT"] = str(root)
    env.pop("GITHUB_COPILOT_SESSION_ID", None)
    proc = subprocess.run(
        [sys.executable, str(HOOKS / "session_start.py")],
        input=json.dumps({"sessionId": "cop-sess-1", "cwd": str(ws)}),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr

    from atelier.core.foundation.paths import workspace_key

    state_path = root / "workspaces" / workspace_key(ws) / "session_state.json"
    assert state_path.is_file()
    assert json.loads(state_path.read_text(encoding="utf-8"))["session_id"] == "cop-sess-1"


def test_copilot_session_start_no_session_id_writes_no_bridge(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    ws = tmp_path / "repo"
    ws.mkdir()
    env = os.environ.copy()
    env["ATELIER_ROOT"] = str(root)
    env.pop("GITHUB_COPILOT_SESSION_ID", None)
    proc = subprocess.run(
        [sys.executable, str(HOOKS / "session_start.py")],
        input=json.dumps({"cwd": str(ws)}),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr

    from atelier.core.foundation.paths import workspace_key

    assert not (root / "workspaces" / workspace_key(ws) / "session_state.json").exists()


def test_copilot_hooks_manifest_wires_failure_hook() -> None:
    data = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    rendered = json.dumps(data)
    assert "postToolUseFailure" in data["hooks"]
    assert "post_tool_use_failure.py" in rendered
