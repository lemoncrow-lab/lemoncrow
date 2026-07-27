"""A stale-code daemon waits for its bridges before restarting.

Exiting under a live bridge drops that host's in-flight calls, and on Cursor a
single failed call ends LemonCrow for the whole window -- the host falls back to
built-ins and never routes back. Running stale code a while longer is far
cheaper. A session that never detaches must still not pin stale code forever.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from lemoncrow.gateway.adapters import mcp_daemon


class _Ticks(threading.Event):
    """A stop-event that yields exactly *ticks* loop iterations, without sleeping.

    ``exhausted`` lets the test await the reaper thread without calling ``wait``
    itself -- doing so would consume the very ticks the reaper is driven by.
    """

    def __init__(self, ticks: int) -> None:
        super().__init__()
        self._left = ticks
        self._lock = threading.Lock()
        self.exhausted = threading.Event()

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            if self._left <= 0:
                self.exhausted.set()
                return True
            self._left -= 1
            return False


def _run_reaper(
    monkeypatch: pytest.MonkeyPatch,
    *,
    attached: int,
    ticks: int = 4,
    drain_seconds: float = 1800.0,
) -> tuple[list[int], SimpleNamespace]:
    exits: list[int] = []
    monkeypatch.setattr(mcp_daemon.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(mcp_daemon.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(mcp_daemon, "_code_fingerprint", lambda: "installed-new")
    monkeypatch.setattr(mcp_daemon, "_startup_code_fingerprint", lambda: "loaded-old")
    # raising=False so this test still EXERCISES a build without the drain cap
    # (proving the behaviour), rather than erroring on the missing attribute.
    monkeypatch.setattr(mcp_daemon, "_STALE_CODE_DRAIN_SECONDS", drain_seconds, raising=False)

    live = mcp_daemon._LiveSessions()
    for n in range(attached):
        live.touch(f"bridge-{n}")
    server = SimpleNamespace(should_exit=False, force_exit=False)
    stop = _Ticks(ticks)

    mcp_daemon._start_idle_reaper(server, live, idle_grace_seconds=0, stop=stop)
    assert stop.exhausted.wait(5.0), "reaper thread did not finish its ticks"
    return exits, server


def test_stale_code_does_not_kill_an_attached_session(monkeypatch: pytest.MonkeyPatch) -> None:
    exits, server = _run_reaper(monkeypatch, attached=1)
    assert exits == [], "restarted while a bridge was still attached"
    assert server.should_exit is False


def test_stale_code_restarts_once_the_last_bridge_detaches(monkeypatch: pytest.MonkeyPatch) -> None:
    exits, server = _run_reaper(monkeypatch, attached=0)
    assert exits and set(exits) == {0}
    assert server.should_exit is True
    assert server.force_exit is True


def test_a_session_that_never_detaches_cannot_pin_stale_code_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits, _server = _run_reaper(monkeypatch, attached=2, drain_seconds=0.0)
    assert exits and set(exits) == {0}, "drain cap must eventually force the restart"


def test_matching_fingerprint_never_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    exits: list[int] = []
    monkeypatch.setattr(mcp_daemon.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(mcp_daemon, "_code_fingerprint", lambda: "same")
    monkeypatch.setattr(mcp_daemon, "_startup_code_fingerprint", lambda: "same")
    live = mcp_daemon._LiveSessions()
    server = SimpleNamespace(should_exit=False, force_exit=False)
    stop = _Ticks(3)

    mcp_daemon._start_idle_reaper(server, live, idle_grace_seconds=0, stop=stop)
    assert stop.exhausted.wait(3.0)

    assert exits == []
