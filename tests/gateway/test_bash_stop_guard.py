"""Stop-hook acknowledgement for MCP-managed Bash commands."""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from types import ModuleType

import pytest

_STOP = Path("integrations/claude/plugin/hooks/stop.py")


def _load_stop() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lemoncrow_test_bash_stop", _STOP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _register_job(
    root: Path,
    workspace: Path,
    *,
    explicit_background: bool,
    command_id: str = "command-1",
) -> None:
    sessions = root / "mcp_sessions"
    sessions.mkdir(parents=True)
    (sessions / "mcp-test.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "workspace": str(workspace),
                "claude_session_id": "claude-test",
                "managed_bash": [
                    {
                        "session_id": command_id,
                        "pid": os.getpid(),
                        "explicit_background": explicit_background,
                        "log_file": "/tmp/lemoncrow-bash/stdout.log",
                        "log_file_stderr": "/tmp/lemoncrow-bash/stderr.log",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _payload(workspace: Path) -> dict[str, str]:
    return {"session_id": "claude-test", "cwd": str(workspace)}


def test_foreground_bash_warns_once_then_allows_stop_for_30_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = _load_stop()
    root = tmp_path / "lc-root"
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    _register_job(root, tmp_path, explicit_background=False)

    first, first_info = stop._bash_stop_guard(_payload(tmp_path))
    assert first is not None and first["decision"] == "block"
    assert "Stop again within 30 seconds" in first["reason"]
    assert "stdout.log" in first["reason"]
    assert first_info == ""

    second, second_info = stop._bash_stop_guard(_payload(tmp_path))
    assert second is None
    assert "process groups will be killed" in second_info


def test_foreground_warning_resets_after_30_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stop = _load_stop()
    root = tmp_path / "lc-root"
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    _register_job(root, tmp_path, explicit_background=False)

    first, _ = stop._bash_stop_guard(_payload(tmp_path))
    assert first is not None
    warning_path = stop._bash_stop_warning_path("claude-test")
    state = json.loads(warning_path.read_text(encoding="utf-8"))
    state["warned_at"] = time.time() - 31
    warning_path.write_text(json.dumps(state), encoding="utf-8")

    expired, _ = stop._bash_stop_guard(_payload(tmp_path))
    assert expired is not None and expired["decision"] == "block"


def test_explicit_background_job_never_blocks_stop_and_shows_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = _load_stop()
    root = tmp_path / "lc-root"
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    _register_job(root, tmp_path, explicit_background=True)

    decision, info = stop._bash_stop_guard(_payload(tmp_path))
    assert decision is None
    assert "preserved after MCP exit" in info
    assert "stdout.log" in info


def test_dead_mcp_server_pid_registration_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hard-killed MCP leaves a registration file behind; its orphaned
    managed_bash rows must not block Stop (regression: stop.py:_running_managed_bash)."""
    stop = _load_stop()
    root = tmp_path / "lc-root"
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    sessions = root / "mcp_sessions"
    sessions.mkdir(parents=True)
    (sessions / "dead.json").write_text(
        json.dumps(
            {
                "pid": 2147483646,  # never-assigned high pid == dead server
                "workspace": str(tmp_path),
                "claude_session_id": "claude-test",
                "managed_bash": [{"session_id": "c1", "pid": os.getpid(), "explicit_background": False}],
            }
        ),
        encoding="utf-8",
    )
    decision, info = stop._bash_stop_guard(_payload(tmp_path))
    assert decision is None
    assert info == ""


def test_reused_command_pid_is_not_treated_as_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaked row whose pid the OS reused for an unrelated process must not
    block Stop: the row's start time predates the live process (regression)."""
    stop = _load_stop()
    root = tmp_path / "lc-root"
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    sessions = root / "mcp_sessions"
    sessions.mkdir(parents=True)
    (sessions / "mcp-test.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "workspace": str(tmp_path),
                "claude_session_id": "claude-test",
                "managed_bash": [
                    {
                        "session_id": "c1",
                        "pid": os.getpid(),
                        "explicit_background": False,
                        "started_at": 1.0,  # 1970 -- long before this process began
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    decision, info = stop._bash_stop_guard(_payload(tmp_path))
    assert decision is None
    assert info == ""


def test_live_command_with_recent_started_at_still_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard against over-rejection: a genuinely running foreground job that
    records started_at must still block Stop once."""
    stop = _load_stop()
    root = tmp_path / "lc-root"
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    sessions = root / "mcp_sessions"
    sessions.mkdir(parents=True)
    (sessions / "mcp-test.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "workspace": str(tmp_path),
                "claude_session_id": "claude-test",
                "managed_bash": [
                    {
                        "session_id": "c1",
                        "pid": os.getpid(),
                        "explicit_background": False,
                        "started_at": time.time(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    decision, _ = stop._bash_stop_guard(_payload(tmp_path))
    assert decision is not None and decision["decision"] == "block"
