"""Tests for action="update": the only way to install an *enforced* kill
deadline on a running managed command. `timeout` at start remains purely a
soft response budget (see test_bash_exec_background_hang_regression.py and
test_shell_background_session_survives_past_timeout in
tests/gateway/test_mcp_tool_handlers.py) -- it never kills anything on its
own; only a deliberate action="update" call (update_managed_command) makes
`timeout` the exact, enforced deadline instead of floored at
_MANAGED_COMMAND_HARD_CAP_S."""

from __future__ import annotations

import time

import pytest

import lemoncrow.pro.capabilities.tool_supervision.bash_exec as bx


def _poll_until_done(session_id: str, timeout_s: float = 10.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = bx.poll_managed_command(session_id)
        if result["status"] != "running":
            return result
        time.sleep(0.02)
    raise AssertionError("managed command did not finish in time")


def test_default_timeout_never_kills_even_when_exceeded() -> None:
    # Soft budget only -- a background job survives well past its `timeout`
    # as long as no action="update" is ever called.
    started = bx.start_managed_command(
        "python3 -c \"import time; time.sleep(1); print('done')\"",
        timeout=0.2,
    )
    sid = str(started["session_id"])
    result = _poll_until_done(sid, timeout_s=5.0)
    assert result["status"] == "completed"
    assert result["stdout"] == "done"


def test_action_update_installs_an_enforced_kill_deadline() -> None:
    started = bx.start_managed_command(
        'python3 -c "import time; time.sleep(30)"',
        timeout=3600,  # soft budget -- would never kill on its own
    )
    sid = str(started["session_id"])
    updated = bx.update_managed_command(sid, timeout=0.3)
    assert updated["status"] == "running"
    assert updated["updated"] is True
    assert updated["timeout"] == 0.3

    start = time.monotonic()
    result = _poll_until_done(sid, timeout_s=5.0)
    elapsed = time.monotonic() - start
    assert elapsed < 3.0
    assert result["status"] == "timed_out"
    assert "timed out" in str(result["stderr"]).lower()


def test_action_update_can_extend_its_own_deadline_again() -> None:
    started = bx.start_managed_command(
        "python3 -c \"import time; time.sleep(1); print('done')\"",
        timeout=10,
    )
    sid = str(started["session_id"])
    bx.update_managed_command(sid, timeout=0.2)  # installs a tight deadline
    extended = bx.update_managed_command(sid, timeout=5)  # moves it back out
    assert extended["updated"] is True
    assert extended["timeout"] == 5.0

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

    result = bx.update_managed_command(sid, timeout=10)
    assert result["updated"] is False
    assert result["status"] == "completed"
    bx.poll_managed_command(sid)  # reap / cleanup


def test_update_managed_command_unknown_session_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        bx.update_managed_command("does-not-exist", timeout=5)


@pytest.mark.parametrize("timeout", [0.2, 5, 90, 3600])
def test_default_and_short_timeouts_keep_the_fixed_hard_cap(timeout: float) -> None:
    # A start timeout at or below the backstop never changes process lifetime.
    started = bx.start_managed_command("sleep 30", timeout=timeout)
    sid = str(started["session_id"])
    try:
        managed = bx._MANAGED_COMMANDS[sid]
        assert bx._effective_deadline_s(managed) == bx._MANAGED_COMMAND_HARD_CAP_S
    finally:
        bx.poll_managed_command(sid, cancel=True)


@pytest.mark.parametrize("timeout", [7200, 21600])
def test_explicit_timeout_beyond_the_cap_is_respected(timeout: float) -> None:
    # A caller who asked to wait LONGER than the backstop must not have the
    # command killed out from under its own waiting call at 1h: the process
    # lives at least `timeout` seconds.
    started = bx.start_managed_command("sleep 30", timeout=timeout)
    sid = str(started["session_id"])
    try:
        managed = bx._MANAGED_COMMANDS[sid]
        assert bx._effective_deadline_s(managed) == timeout
    finally:
        bx.poll_managed_command(sid, cancel=True)


def test_update_is_clamped_to_the_max_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bx, "_MAX_EXPLICIT_TIMEOUT_S", 2.0)
    started = bx.start_managed_command("sleep 5", timeout=5)
    sid = str(started["session_id"])
    try:
        updated = bx.update_managed_command(sid, timeout=999.0)
        assert updated["timeout"] == 2.0
        # Re-clamped however many times it's extended.
        updated_again = bx.update_managed_command(sid, timeout=999.0)
        assert updated_again["timeout"] == 2.0
    finally:
        bx.poll_managed_command(sid, cancel=True)


def test_shutdown_cleanup_kills_only_non_explicit_background() -> None:
    foreground = bx.start_managed_command("sleep 30")
    background = bx.start_managed_command("sleep 30", explicit_background=True)
    foreground_id = str(foreground["session_id"])
    background_id = str(background["session_id"])
    try:
        summary = bx.cleanup_managed_commands()
        assert [row["session_id"] for row in summary["terminated"]] == [foreground_id]
        assert [row["session_id"] for row in summary["preserved"]] == [background_id]
        assert bx._MANAGED_COMMANDS[foreground_id].proc.poll() is not None
        assert bx._MANAGED_COMMANDS[background_id].proc.poll() is None
    finally:
        for session_id in (foreground_id, background_id):
            try:
                bx.poll_managed_command(session_id, cancel=True)
            except KeyError:
                pass
