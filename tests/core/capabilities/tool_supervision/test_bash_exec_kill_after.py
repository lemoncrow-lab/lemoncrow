"""Tests for the explicit hard-kill deadline feature: `kill_after` at launch
(a real, enforced kill -- distinct from `timeout`, which is only a soft
response budget, see _MANAGED_COMMAND_HARD_CAP_S) and action="update" to move
a running command's deadline (update_managed_command)."""

from __future__ import annotations

import time

import pytest

import atelier.core.capabilities.tool_supervision.bash_exec as bx


def _poll_until_done(session_id: str, timeout_s: float = 10.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = bx.poll_managed_command(session_id)
        if result["status"] != "running":
            return result
        time.sleep(0.02)
    raise AssertionError("managed command did not finish in time")


def test_kill_after_kills_before_default_hard_cap_and_before_large_timeout() -> None:
    # `timeout` (soft) and the default hard cap are both far larger than
    # kill_after -- only the explicit kill_after should be honored.
    started = bx.start_managed_command(
        'python3 -c "import time; time.sleep(30)"',
        timeout=3600,
        kill_after=0.3,
    )
    sid = str(started["session_id"])
    assert started["kill_after"] == 0.3
    start = time.monotonic()
    result = _poll_until_done(sid, timeout_s=5.0)
    elapsed = time.monotonic() - start
    assert elapsed < 3.0
    assert result["status"] == "timed_out"
    assert "timed out" in str(result["stderr"]).lower()


def test_action_update_extends_a_running_kill_after_deadline() -> None:
    started = bx.start_managed_command(
        "python3 -c \"import time; time.sleep(1); print('done')\"",
        timeout=10,
        kill_after=0.3,
    )
    sid = str(started["session_id"])
    updated = bx.update_managed_command(sid, kill_after=5)
    assert updated["status"] == "running"
    assert updated["updated"] is True
    assert updated["kill_after"] == 5.0

    result = _poll_until_done(sid, timeout_s=5.0)
    assert result["status"] == "completed"
    assert result["stdout"] == "done"


def test_update_managed_command_on_finished_but_unreaped_session_reports_not_updated() -> None:
    started = bx.start_managed_command("echo hi", timeout=5)
    sid = str(started["session_id"])
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        snap = bx.peek_managed_command(sid)  # never reaps
        if snap["status"] != "running":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("command did not finish in time")

    result = bx.update_managed_command(sid, kill_after=10)
    assert result["updated"] is False
    assert result["status"] == "completed"
    bx.poll_managed_command(sid)  # reap / cleanup


def test_update_managed_command_unknown_session_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        bx.update_managed_command("does-not-exist", kill_after=5)


def test_kill_after_is_clamped_to_the_max_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bx, "_MAX_KILL_AFTER_S", 100.0)
    started = bx.start_managed_command("sleep 5", timeout=5, kill_after=99999.0)
    sid = str(started["session_id"])
    try:
        assert started["kill_after"] == 100.0
        updated = bx.update_managed_command(sid, kill_after=99999.0)
        assert updated["kill_after"] == 100.0
    finally:
        bx.poll_managed_command(sid, cancel=True)


def test_peek_and_poll_expose_kill_after_remaining_only_when_explicitly_set() -> None:
    plain = bx.start_managed_command("sleep 2", timeout=10)
    plain_sid = str(plain["session_id"])
    with_deadline = bx.start_managed_command("sleep 2", timeout=10, kill_after=30)
    deadline_sid = str(with_deadline["session_id"])
    try:
        assert "kill_after_remaining_ms" not in bx.peek_managed_command(plain_sid)
        assert "kill_after_remaining_ms" not in bx.poll_managed_command(plain_sid)

        peek = bx.peek_managed_command(deadline_sid)
        assert 0 < peek["kill_after_remaining_ms"] <= 30_000
        polled = bx.poll_managed_command(deadline_sid)
        assert 0 < polled["kill_after_remaining_ms"] <= 30_000
    finally:
        bx.poll_managed_command(plain_sid, cancel=True)
        bx.poll_managed_command(deadline_sid, cancel=True)
