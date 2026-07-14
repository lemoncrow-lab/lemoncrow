"""Tests for interactive sessions (bash interactive=true + action="send"):
a long-lived REPL with a live stdin pipe and a sliding idle-TTL kill window,
so one heavy-import interpreter (mujoco, torch, ...) is reused across calls
instead of paying the import tax per one-shot script. `timeout` semantics are
untouched -- the idle TTL is a third deadline mode in _effective_deadline_s,
beneath an explicit action="update" deadline."""

from __future__ import annotations

import time

import pytest

import lemoncrow.pro.capabilities.tool_supervision.bash_exec as bx


def _open_python_session(idle_ttl: float = 30.0) -> str:
    started = bx.start_managed_command(
        "python3 -u -i -q",
        interactive=True,
        idle_ttl=idle_ttl,
    )
    assert started["status"] == "running"
    assert started["interactive"] is True
    return str(started["session_id"])


def _cancel(session_id: str) -> None:
    try:
        bx.poll_managed_command(session_id, cancel=True)
    except KeyError:
        pass


def _poll_until_terminal(session_id: str, timeout_s: float = 10.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = bx.poll_managed_command(session_id)
        if result["status"] != "running":
            return result
        time.sleep(0.05)
    raise AssertionError("session did not reach a terminal state in time")


def test_state_persists_across_sends() -> None:
    sid = _open_python_session()
    try:
        first = bx.send_managed_input(sid, "x = 41", wait=10.0)
        assert first["status"] == "running"
        assert first["sent"] is True
        second = bx.send_managed_input(sid, "print(x + 1)", wait=10.0)
        assert "42" in str(second["stdout"])
    finally:
        _cancel(sid)


def test_send_returns_only_the_delta() -> None:
    sid = _open_python_session()
    try:
        first = bx.send_managed_input(sid, "print('first-marker')", wait=10.0)
        assert "first-marker" in str(first["stdout"])
        second = bx.send_managed_input(sid, "print('second-marker')", wait=10.0)
        assert "second-marker" in str(second["stdout"])
        assert "first-marker" not in str(second["stdout"])
    finally:
        _cancel(sid)


def test_idle_ttl_kills_an_unused_session() -> None:
    started = bx.start_managed_command("python3 -u -i -q", interactive=True, idle_ttl=1.0)
    sid = str(started["session_id"])
    result = _poll_until_terminal(sid, timeout_s=10.0)
    assert result["status"] == "timed_out"
    assert "idle-expired" in str(result["stderr"])


def test_send_resets_the_idle_clock() -> None:
    started = bx.start_managed_command("python3 -u -i -q", interactive=True, idle_ttl=1.5)
    sid = str(started["session_id"])
    try:
        # Keep poking well past the TTL: the sliding window must keep it alive.
        for _ in range(4):
            time.sleep(0.6)
            result = bx.send_managed_input(sid, "1 + 1", wait=5.0)
            assert result["status"] == "running"
        # 4 * 0.6s = 2.4s elapsed > idle_ttl -- alive only because sends reset it.
        snap = bx.peek_managed_command(sid)
        assert snap["status"] == "running"
    finally:
        _cancel(sid)


def test_empty_send_drains_output_that_arrived_later() -> None:
    sid = _open_python_session()
    try:
        bx.send_managed_input(sid, "import time", wait=10.0)
        # Kick off a slow statement but only wait 50ms for its output.
        rushed = bx.send_managed_input(sid, "time.sleep(1); print('slow-marker')", wait=0.05)
        assert "slow-marker" not in str(rushed["stdout"])
        drained = bx.send_managed_input(sid, "", wait=10.0)
        assert drained["sent"] is False
        assert "slow-marker" in str(drained["stdout"])
    finally:
        _cancel(sid)


def test_send_input_is_policy_gated() -> None:
    sid = _open_python_session()
    try:
        result = bx.send_managed_input(sid, "git reset --hard", wait=5.0)
        assert result["blocked"] is True
        assert result["blocked_reason"]
        # The session itself is untouched and still usable.
        follow_up = bx.send_managed_input(sid, "print('still-alive')", wait=10.0)
        assert "still-alive" in str(follow_up["stdout"])
    finally:
        _cancel(sid)


def test_send_to_non_interactive_session_raises() -> None:
    started = bx.start_managed_command("sleep 5")
    sid = str(started["session_id"])
    try:
        with pytest.raises(ValueError, match="not interactive"):
            bx.send_managed_input(sid, "echo hi")
    finally:
        _cancel(sid)


def test_send_to_unknown_session_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        bx.send_managed_input("does-not-exist", "print(1)")


def test_send_reports_exit_when_the_child_dies() -> None:
    sid = _open_python_session()
    try:
        result = bx.send_managed_input(sid, "raise SystemExit(3)", wait=10.0)
        assert result["status"] != "running"
        assert result["exit_code"] == 3
    finally:
        _cancel(sid)


def test_shutdown_cleanup_kills_interactive_sessions() -> None:
    sid = _open_python_session()
    try:
        summary = bx.cleanup_managed_commands()
        assert sid in [row["session_id"] for row in summary["terminated"]]
        assert bx._MANAGED_COMMANDS[sid].proc.poll() is not None
    finally:
        _cancel(sid)


def test_explicit_update_deadline_wins_over_idle_ttl() -> None:
    started = bx.start_managed_command("python3 -u -i -q", interactive=True, idle_ttl=60.0)
    sid = str(started["session_id"])
    bx.update_managed_command(sid, timeout=0.3)
    result = _poll_until_terminal(sid, timeout_s=10.0)
    assert result["status"] == "timed_out"
    assert "timed out" in str(result["stderr"]).lower()


def test_idle_ttl_is_clamped_to_sane_bounds() -> None:
    low = bx.start_managed_command("sleep 30", interactive=True, idle_ttl=0.0)
    high = bx.start_managed_command("sleep 30", interactive=True, idle_ttl=10**9)
    low_id, high_id = str(low["session_id"]), str(high["session_id"])
    try:
        assert bx._MANAGED_COMMANDS[low_id].idle_ttl == 1.0
        assert bx._MANAGED_COMMANDS[high_id].idle_ttl == bx._MANAGED_COMMAND_HARD_CAP_S
    finally:
        _cancel(low_id)
        _cancel(high_id)
