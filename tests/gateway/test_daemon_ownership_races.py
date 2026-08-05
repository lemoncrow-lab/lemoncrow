"""Two daemons for one workspace must never trample each other's state.

Regression cover for the ``MCP daemon HTTP 403`` outage: both daemons heartbeat
the shared registration file, so the one that did NOT own the socket rewrote it
with its own bearer token and every authed ``POST /mcp`` came back 403. The
bridge's 403 retry re-ensured the daemon, but liveness was validated with the
unauthenticated ``/healthz`` route -- answered fine by the socket owner -- so it
never respawned. And when the losing daemon finally exited, its teardown deleted
the winner's registration and unlinked the winner's socket.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.gateway.adapters import mcp_bridge as mb
from lemoncrow.gateway.adapters import mcp_daemon as md


def _write_foreign_registration(
    root: Path, ws_hash: str, *, pid: int, token: str, socket_path: str = "/x.sock"
) -> Path:
    """Plant a registration naming *pid* (a daemon that is not this process)."""
    path = md.daemon_registration_path(root, ws_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "socket": socket_path, "token": token}), encoding="utf-8")
    return path


def _listening_socket(path: Path) -> socket.socket:
    """A daemon-like AF_UNIX listener bound to *path*."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    return sock


class _Ticks(threading.Event):
    """A stop-event that yields exactly *ticks* loop iterations, without sleeping."""

    def __init__(self, ticks: int) -> None:
        super().__init__()
        self._left = ticks
        self._lock = threading.Lock()

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            if self._left <= 0:
                return True
            self._left -= 1
            return False


def _run_heartbeat(root: Path, ws_hash: str, *, token: str, ticks: int = 3) -> None:
    """Start the heartbeat loop, drive it *ticks* times, and wait for it to end."""
    md._start_heartbeat(
        root,
        ws_hash,
        socket_path="/x.sock",
        token=token,
        workspace=str(root),
        stop=_Ticks(ticks),
    )
    # ``start()`` publishes the thread before returning and it disappears from
    # enumerate() once the loop ends, so "not found" already means "finished" --
    # the stand-down path can get there before we look.
    worker = next((t for t in threading.enumerate() if t.name == "mcp-daemon-heartbeat"), None)
    if worker is None:
        return
    worker.join(5.0)
    assert not worker.is_alive(), "heartbeat thread never finished"


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeDaemon:
    """A daemon behind the socket: ``/healthz`` open, ``/session/ping`` authed.

    Doubles as the ``daemon_client`` factory so ``_probe_healthy`` can be driven
    without a real process.
    """

    def __init__(self, *, token: str, healthz: int = 200) -> None:
        self._token = token
        self._healthz = healthz
        self.presented: list[str] = []

    def __call__(self, reg: dict[str, Any], *, timeout: float = 2.0) -> _FakeDaemon:
        return self

    def __enter__(self) -> _FakeDaemon:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get(self, url: str) -> _FakeResponse:
        assert url.endswith(md._HEALTHZ_PATH)
        return _FakeResponse(self._healthz)

    def post(self, url: str, headers: dict[str, str]) -> _FakeResponse:
        assert url.endswith(md._SESSION_PING_PATH)
        token = headers["Authorization"].removeprefix("Bearer ")
        self.presented.append(token)
        return _FakeResponse(200 if token == self._token else 403)


# ------------------------------------------------------- (a) teardown ownership


def test_shutdown_keeps_a_registration_owned_by_another_pid(tmp_path: Path) -> None:
    """The loser exiting must not delete the live winner's registration."""
    other = os.getppid()  # a live pid that is not this process
    path = _write_foreign_registration(tmp_path, "ws", pid=other, token="theirs")

    md._remove_registration(tmp_path, "ws")

    assert path.exists(), "deleted the live owner's registration"
    assert json.loads(path.read_text(encoding="utf-8")) == {"pid": other, "socket": "/x.sock", "token": "theirs"}


def test_shutdown_removes_a_registration_this_process_owns(tmp_path: Path) -> None:
    md._write_registration(tmp_path, "ws", socket_path="/x.sock", token="mine", workspace=str(tmp_path))
    md._remove_registration(tmp_path, "ws")
    assert not md.daemon_registration_path(tmp_path, "ws").exists()


def test_shutdown_leaves_a_socket_owned_by_another_daemon(tmp_path: Path) -> None:
    """Lost the registration -> a rival owns the path now; leave it alone.

    The inode check cannot carry this alone: the filesystem happily hands the
    rival the very same inode number right after our unlink.
    """
    sock_path = tmp_path / "d.sock"
    ours = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        ours.bind(str(sock_path))
        bound = md._socket_identity(sock_path)

        assert md._unlink_owned_socket(sock_path, bound, still_registered=False) is False
        assert sock_path.exists(), "unlinked the live daemon's socket"
    finally:
        ours.close()


def test_shutdown_leaves_a_socket_whose_inode_moved(tmp_path: Path) -> None:
    """Still registered, but the path is not the inode we bound -> hands off."""
    ours = tmp_path / "ours.sock"
    theirs = tmp_path / "theirs.sock"
    sockets = [socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) for _ in range(2)]
    try:
        sockets[0].bind(str(ours))
        sockets[1].bind(str(theirs))

        assert md._unlink_owned_socket(theirs, md._socket_identity(ours), still_registered=True) is False
        assert theirs.exists()
    finally:
        for s in sockets:
            s.close()


def test_shutdown_unlinks_the_socket_this_process_bound(tmp_path: Path) -> None:
    sock_path = tmp_path / "d.sock"
    ours = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        ours.bind(str(sock_path))
        bound = md._socket_identity(sock_path)
        assert md._unlink_owned_socket(sock_path, bound, still_registered=True) is True
        assert not sock_path.exists()
    finally:
        ours.close()


# ------------------------------------------------------ (b) heartbeat ownership


def test_heartbeat_stands_down_when_another_live_daemon_owns_the_registration(tmp_path: Path) -> None:
    """The exact token-poisoning step of the 403 outage."""
    other = os.getppid()
    path = _write_foreign_registration(tmp_path, "ws", pid=other, token="theirs")

    _run_heartbeat(tmp_path, "ws", token="mine")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pid"] == other
    assert data["token"] == "theirs", "heartbeat overwrote the live daemon's bearer token"


def test_heartbeat_refreshes_a_registration_this_process_owns(tmp_path: Path) -> None:
    md._write_registration(tmp_path, "ws", socket_path="/x.sock", token="mine", workspace=str(tmp_path))
    path = md.daemon_registration_path(tmp_path, "ws")
    before = json.loads(path.read_text(encoding="utf-8"))["last_heartbeat"]
    time.sleep(0.01)

    _run_heartbeat(tmp_path, "ws", token="mine")

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["pid"] == os.getpid()
    assert after["token"] == "mine"
    assert after["last_heartbeat"] > before


def test_heartbeat_reclaims_a_registration_naming_a_dead_pid(tmp_path: Path) -> None:
    """Fail-open: the ownership guard must not wedge recovery after a crash."""
    path = _write_foreign_registration(tmp_path, "ws", pid=2**31 - 1, token="ghost")

    written = md._write_registration(
        tmp_path, "ws", socket_path="/x.sock", token="mine", workspace=str(tmp_path), only_if_owner=True
    )

    assert written is True
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "mine"


# ------------------------------------------------ (c) authenticated liveness


def test_probe_rejects_a_daemon_that_403s_the_registered_token(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = _FakeDaemon(token="real")
    monkeypatch.setattr(md, "daemon_client", daemon)

    assert md._probe_healthy({"socket": "/x.sock", "token": "real"}) is True
    assert md._probe_healthy({"socket": "/x.sock", "token": "clobbered"}) is False
    assert daemon.presented == ["real", "clobbered"]  # /healthz alone is never enough
    assert md._probe_healthy({"socket": "/x.sock"}) is False  # no token -> unusable


def test_probe_rejects_a_daemon_whose_health_route_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = _FakeDaemon(token="real", healthz=503)
    monkeypatch.setattr(md, "daemon_client", daemon)

    assert md._probe_healthy({"socket": "/x.sock", "token": "real"}) is False
    assert daemon.presented == []  # short-circuits before the authed probe


def test_ensure_daemon_respawns_when_the_registered_token_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A daemon whose registration token 403s is not usable -- respawn, don't reuse."""
    poisoned = {"pid": os.getpid(), "socket": "/x.sock", "token": "clobbered"}
    fresh = {"pid": os.getpid(), "socket": "/x.sock", "token": "real"}
    monkeypatch.setattr(md, "read_daemon_registration", lambda *_a, **_k: poisoned)
    monkeypatch.setattr(md, "daemon_client", _FakeDaemon(token="real"))
    spawned: list[str] = []

    def _spawn(workspace: str, root: Path, ws_hash: str, idle_grace_seconds: float | None) -> dict[str, Any]:
        spawned.append(workspace)
        return fresh

    monkeypatch.setattr(md, "_spawn_daemon", _spawn)

    assert md.ensure_daemon(str(tmp_path), tmp_path) is fresh
    assert spawned == [str(tmp_path)]


def test_ensure_daemon_reuses_a_daemon_that_accepts_the_registered_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live = {"pid": os.getpid(), "socket": "/x.sock", "token": "real"}
    monkeypatch.setattr(md, "read_daemon_registration", lambda *_a, **_k: live)
    monkeypatch.setattr(md, "daemon_client", _FakeDaemon(token="real"))
    monkeypatch.setattr(md, "_spawn_daemon", lambda *_a, **_k: pytest.fail("respawned a usable daemon"))

    assert md.ensure_daemon(str(tmp_path), tmp_path) is live


def test_client_pool_rebinds_when_the_daemon_pid_changes() -> None:
    """A replacement daemon rebinds the SAME path, so the path alone cannot key the
    client: a pooled keep-alive connection kept talking to the vacated daemon and
    the 403 retry re-sent the new token to the old process forever."""
    built: list[Any] = []

    class _FakeClient:
        def __init__(self, pid: Any) -> None:
            self.pid = pid

        def close(self) -> None:
            pass

    def _factory(reg: dict[str, Any], *, timeout: Any) -> _FakeClient:
        built.append(reg.get("pid"))
        return _FakeClient(reg.get("pid"))

    old = {"socket": "/run/user/1000/a.sock", "pid": 111}
    pool = mb._ClientPool(old, _factory, timeout=None)
    assert pool.for_registration(dict(old)) is pool.for_registration(dict(old))
    assert built == [111]

    respawned = {"socket": "/run/user/1000/a.sock", "pid": 222}  # same path, new daemon
    assert pool.for_registration(respawned).pid == 222
    assert built == [111, 222]


# ---------------------------------------------------------- (d) prune ownership


def test_prune_keeps_the_socket_a_live_successor_is_serving(tmp_path: Path) -> None:
    """A dead pid in the registration says nothing about who owns the socket.

    The path is deterministic per workspace, so the successor that replaced the
    crashed daemon is bound to the very path the dead registration names --
    unlinking it strands every bridge for that workspace on a dead path.
    """
    sock_path = tmp_path / "d.sock"
    listener = _listening_socket(sock_path)
    try:
        reg = _write_foreign_registration(tmp_path, "ws", pid=2**31 - 1, token="ghost", socket_path=str(sock_path))

        assert md.prune_stale_daemons(tmp_path) == 1

        assert not reg.exists(), "kept the dead pid's registration"
        assert sock_path.exists(), "unlinked the live successor's socket"
        assert md._socket_has_listener(str(sock_path)) is True
    finally:
        listener.close()


def test_prune_unlinks_a_genuinely_orphaned_socket(tmp_path: Path) -> None:
    """Nothing answers on it and the pid is dead -> it really is leftover."""
    sock_path = tmp_path / "d.sock"
    _listening_socket(sock_path).close()  # crash: the file outlives the listener
    assert sock_path.exists() and md._socket_has_listener(str(sock_path)) is False
    reg = _write_foreign_registration(tmp_path, "ws", pid=2**31 - 1, token="ghost", socket_path=str(sock_path))

    assert md.prune_stale_daemons(tmp_path) == 1

    assert not reg.exists()
    assert not sock_path.exists(), "left an orphaned socket behind"
