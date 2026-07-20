"""Idle-aware inline wait: a foreground bash call hands back a running handle
early once the command has stalled (no output/CPU/IO progress) past the grace
window, but a command that keeps making progress runs to completion.

Exercises the synchronous inline path of ``_run_bash_tool`` (a direct call never
sets the deferral context, so it does not take the deferred branch). The floor/
grace/tick are shrunk via monkeypatch so the behaviour is observable in ~1s.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time

import pytest

from lemoncrow.gateway.adapters.mcp import bash


def _kill(result: dict[str, object]) -> None:
    session_id = result.get("session_id")
    if isinstance(session_id, str) and session_id:
        with contextlib.suppress(Exception):
            bash._run_bash_tool(action="kill", session_id=session_id)


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not os.path.isdir("/proc"),
    reason="idle early-return needs the /proc CPU/IO signal (Linux only)",
)
def test_idle_command_returns_running_handle_early(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_BASH_IDLE_RETURN", "1")
    monkeypatch.setattr(bash, "_BASH_IDLE_FLOOR_S", 0.0)
    monkeypatch.setattr(bash, "_BASH_IDLE_GRACE_S", 0.3)
    monkeypatch.setattr(bash, "_BASH_IDLE_TICK_S", 0.05)

    start = time.monotonic()
    # sleep: no output, ~0 CPU, ~0 IO -- the deadlock/idle shape. timeout(30) and
    # the sleep(20) both far exceed the ~0.3s grace, so an early return is the
    # only way this call comes back quickly.
    result = bash._run_bash_tool(command="sleep 20", timeout=30, action="run")
    elapsed = time.monotonic() - start
    try:
        assert result.get("status") == "running", result
        assert isinstance(result.get("session_id"), str) and result["session_id"]
        assert elapsed < 5.0, f"idle command not cut early: {elapsed:.2f}s"
    finally:
        _kill(result)


def test_busy_command_is_not_cut_short(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_BASH_IDLE_RETURN", "1")
    monkeypatch.setattr(bash, "_BASH_IDLE_FLOOR_S", 0.0)
    monkeypatch.setattr(bash, "_BASH_IDLE_GRACE_S", 0.3)
    monkeypatch.setattr(bash, "_BASH_IDLE_TICK_S", 0.05)

    # ~1.2s of pure CPU: utime keeps climbing every tick, so the idle timer keeps
    # refreshing and the command runs to completion instead of being cut early.
    cmd = 'python3 -c "import time; end=time.time()+1.2\nwhile time.time() < end: pass"'
    result = bash._run_bash_tool(command=cmd, timeout=30, action="run")
    try:
        assert result.get("status") != "running", result
        assert result.get("exit_code") == 0, result
    finally:
        _kill(result)
