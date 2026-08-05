"""Per-workspace singleton MCP daemon (loopback HTTP) + spawn/lifecycle.

One long-lived daemon per workspace serves every host session (Claude, Codex,
OpenCode) for that repo over loopback HTTP, replacing N heavy per-session stdio
processes with N thin stdio bridges (see ``mcp_bridge.py``) + 1 shared daemon.
The daemon does the heavy startup (code-index warm, embedder pre-load, zoekt
webserver) exactly once and holds the shared caches; bridges forward JSON-RPC
over HTTP and stay near-zero-cost.

Identity: keyed by ``workspace_hash`` under ``<root>/mcp_daemons/<hash>.json``
(pid, loopback port, bearer token, heartbeat). A POSIX file lock guards the
find-or-spawn race between concurrent bridges so a workspace never ends up with
two daemons.

Security: binds ``127.0.0.1`` on an ephemeral port and requires a per-daemon
bearer token (random, written 0600) on every ``/mcp`` call, so nothing on the
machine can drive the tool surface without reading the registration file.

This module is additive: it reuses ``mcp_http.register_mcp_http`` and
``mcp_server._handle`` unchanged, and touches none of the stdio ``serve()``
path.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.paths import default_store_root, pin_workspace_env
from lemoncrow.core.foundation.session_window import workspace_hash

_log = logging.getLogger("lemoncrow.mcp")

_DAEMON_DIRNAME = "mcp_daemons"
# A daemon with no live tool traffic for this long shuts itself down, freeing the
# resident index/embedder/threads. The next bridge respawns it on demand.
_DEFAULT_IDLE_GRACE_SECONDS = 600.0
# How long ``ensure_daemon`` waits for a freshly spawned daemon to register and
# answer a health probe before giving up (cold start warms the index off the hot
# path, so the port is usually listenable within a second or two).
_SPAWN_HEALTH_TIMEOUT_SECONDS = 30.0
_HEARTBEAT_INTERVAL_SECONDS = 30.0
# Bound on the local connect() used to tell a live daemon's socket from the
# orphan a crashed one left behind (see _socket_has_listener).
_SOCKET_CONNECT_PROBE_SECONDS = 0.5
# A bridge that stops pinging for longer than this is treated as detached (covers
# an ungraceful bridge death that never sent /session/close). Must exceed the
# bridge ping interval (see mcp_bridge._PING_INTERVAL_SECONDS) with margin.
_SESSION_TTL_SECONDS = 90.0
# How long a stale-code daemon waits for its last bridge to detach before
# restarting anyway. Exiting under a live bridge drops that host's in-flight
# calls, and on Cursor one failed call ends LemonCrow for the whole window --
# the host falls back to built-ins and never routes back. Waiting is therefore
# far cheaper than restarting; but a session that never detaches must not pin
# stale code forever, hence the cap.
_STALE_CODE_DRAIN_SECONDS = float(os.environ.get("LEMONCROW_MCP_STALE_DRAIN_SECONDS", "1800"))
_HEALTHZ_PATH = "/healthz"
_SESSION_PING_PATH = "/session/ping"
_SESSION_CLOSE_PATH = "/session/close"
_BRIDGE_HEADER = "x-lemoncrow-bridge"


# ── paths ────────────────────────────────────────────────────────────────────


def _daemon_dir(root: Path) -> Path:
    return Path(root) / _DAEMON_DIRNAME


def daemon_registration_path(root: Path, ws_hash: str) -> Path:
    """Registration file for the daemon bound to *ws_hash*."""
    return _daemon_dir(root) / f"{ws_hash}.json"


def _daemon_lock_path(root: Path, ws_hash: str) -> Path:
    return _daemon_dir(root) / f"{ws_hash}.lock"


def _daemon_startup_lock_path(root: Path, ws_hash: str) -> Path:
    """Separate lock for ``run_daemon``'s own self-check (see its call site).

    Must NOT be ``_daemon_lock_path``: ``ensure_daemon`` holds that lock for its
    entire find-or-spawn call, including the up-to-30s wait for the spawned
    child to become healthy. If the child also locked that same file before
    binding, it would deadlock against its own still-waiting parent.
    """
    return _daemon_dir(root) / f"{ws_hash}.startup.lock"


def _daemon_log_path(root: Path, ws_hash: str) -> Path:
    return _daemon_dir(root) / f"{ws_hash}.log"


def _daemon_socket_path(root: Path, ws_hash: str) -> Path:
    """Absolute path of the daemon's Unix-domain socket (loopback IPC, no port).

    AF_UNIX paths are capped at ~108 bytes, and ``<root>/mcp_daemons/<ws_hash>``
    can blow past that (deep store roots + up to 120-char workspace slugs). Anchor
    the socket in a short per-user runtime dir keyed by a digest of the identity
    instead.

    Derived from on-disk facts only, never from the ambient environment: reading
    ``XDG_RUNTIME_DIR``/``TMPDIR`` here made the path differ between a daemon
    started from a login shell and its own successor respawned by a bridge that
    inherited a bare env -- one workspace, two socket paths, and every bridge
    pinned to the vacated one answered "MCP daemon unreachable" until the host
    restarted it. ``/run/user/<uid>`` is the same directory for every process of
    that user; only when it does not exist (macOS, containers) does the temp dir
    decide, and the bridge re-dials whatever path the registration reports.
    """
    digest = hashlib.sha256(ws_hash.encode("utf-8")).hexdigest()[:16]
    uid = os.getuid() if hasattr(os, "getuid") else 0
    runtime_dir = Path(f"/run/user/{uid}")
    base = runtime_dir if runtime_dir.is_dir() else Path(tempfile.gettempdir())
    return base / f"lemoncrow-mcp-{uid}" / f"{digest}.sock"


# ── liveness helpers ─────────────────────────────────────────────────────────


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _registration_owner(path: Path) -> int | None:
    """The *live* pid recorded in the registration at *path*, else ``None``.

    ``None`` covers every case where no other daemon is currently claiming the
    file: missing, unreadable, or naming a pid that has since died.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = data.get("pid") if isinstance(data, dict) else None
    if not isinstance(pid, int) or not _pid_alive(pid):
        return None
    return pid


def _socket_identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of *path*, or ``None`` when it cannot be stat'ed."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _socket_has_listener(path: str) -> bool:
    """True unless *path* is provably an orphan nobody is serving.

    ``connect()`` on an AF_UNIX stream socket is the cheap positive liveness
    signal: it succeeds only against a live listener, while the socket file a
    crashed daemon left behind refuses with ``ECONNREFUSED``. A timeout means a
    listener is there but its backlog is full, so it counts as alive -- the
    caller unlinks only on evidence of orphanhood, never on the absence of a
    reply.
    """
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(_SOCKET_CONNECT_PROBE_SECONDS)
        client.connect(path)
    except TimeoutError:
        return True
    except OSError:
        return False
    finally:
        client.close()
    return True


def _unlink_owned_socket(path: Path, bound: tuple[int, int] | None, *, still_registered: bool) -> bool:
    """Unlink *path* only while this process is still this workspace's daemon.

    Deleting the socket a *live* daemon serves strands every attached bridge on a
    path nothing listens to, so two independent guards apply -- neither is
    sufficient alone:

    * ``still_registered``: the registration file is claimed together with the
      socket bind (under the startup lock), so a file that no longer names this
      pid means a rival took the workspace over and owns whatever is at *path*.
    * inode identity: the path we bound may already have been replaced. Necessary
      but not sufficient -- the filesystem is free to hand a rival the very same
      inode number right after our unlink.
    """
    if not still_registered:
        return False
    if bound is None or _socket_identity(path) != bound:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def read_daemon_registration(root: Path, ws_hash: str) -> dict[str, Any] | None:
    """Return the live registration for *ws_hash*, or ``None``.

    A registration whose pid is dead (crash without cleanup) is treated as
    absent so the caller respawns.
    """
    path = daemon_registration_path(root, ws_hash)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if not isinstance(pid, int) or not _pid_alive(pid):
        return None
    sock = data.get("socket")
    if not isinstance(sock, str) or not sock:
        return None
    return data


_UDS_BASE_URL = "http://lemoncrow-daemon"


def daemon_client(reg: dict[str, Any], *, timeout: Any = 2.0) -> Any:
    """An HTTP client pinned to the daemon's Unix-domain socket.

    A UDS transport bypasses HTTP proxy env entirely (``HTTP_PROXY``/``ALL_PROXY``
    apply only to real host:port URLs), so a proxied host -- e.g. a benchmark
    container behind mitmproxy -- can never hijack this loopback IPC. ``trust_env``
    is off for the same reason. The URL host in requests is a placeholder; the
    transport routes to the socket regardless.
    """
    from lemoncrow.infra.ipc.httpx_uds import uds_http_client

    return uds_http_client(str(reg["socket"]), timeout=timeout)


def _probe_healthy(reg: dict[str, Any], *, timeout: float = 2.0) -> bool:
    """True if the daemon answers *and* accepts the token recorded in *reg*.

    Liveness alone is not enough. When two daemons briefly served one workspace,
    both rewrote the shared registration with their own bearer token, so the file
    could name a token the socket owner rejects. ``/healthz`` is unauthenticated
    and still answered 200, ``ensure_daemon`` called that daemon usable, and every
    authed ``POST /mcp`` came back 403 forever because the respawn path never ran.
    So the probe also POSTs ``/session/ping`` with the registration's token: a
    401/403 means these credentials are unusable, i.e. respawn.

    The ping carries no bridge header, so it registers no phantom session.
    """
    token = reg.get("token")
    if not isinstance(token, str) or not token:
        return False
    try:
        with daemon_client(reg, timeout=timeout) as client:
            resp = client.get(_UDS_BASE_URL + _HEALTHZ_PATH)
            if resp.status_code != 200:
                return False
            authed = client.post(
                _UDS_BASE_URL + _SESSION_PING_PATH,
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception:
        return False
    return authed.status_code == 200


# ── spawn-race lock ──────────────────────────────────────────────────────────


class _FileLock:
    """Best-effort POSIX advisory lock (``flock``) for the spawn critical section.

    Only ever held for the few milliseconds of a find-or-spawn, so contention is
    negligible. Fail-open: if locking is unavailable the caller still
    double-checks the registration, so at worst two daemons race and the second
    to bind loses the port and exits.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> _FileLock:
        import fcntl

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except OSError:
            _log.debug("daemon spawn lock unavailable; proceeding without it", exc_info=True)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None


# ── find-or-spawn (bridge side) ──────────────────────────────────────────────


def _code_fingerprint() -> str | None:
    """Identity of the installed code a daemon runs (mtime_ns of a sentinel).

    Any (re)install rewrites ``mcp_server.py``, so its mtime_ns changes exactly
    when the code a fresh daemon would load differs from what a running daemon
    loaded. ``None`` (unreadable) fails open: never force a restart on it.
    """
    try:
        from lemoncrow.gateway.adapters import mcp_server

        return str(os.stat(mcp_server.__file__).st_mtime_ns)
    except (OSError, ImportError, AttributeError):
        return None


_STARTUP_CODE_FINGERPRINT: str | None = None
_startup_fingerprint_lock = threading.Lock()


def _startup_code_fingerprint() -> str | None:
    """The fingerprint of the code THIS process loaded, pinned at first call.

    Heartbeat rewrites must keep recording what the daemon actually runs, not
    whatever a later reinstall put on disk -- otherwise the staleness check
    compares new-disk to new-disk and never fires.
    """
    global _STARTUP_CODE_FINGERPRINT
    with _startup_fingerprint_lock:
        if _STARTUP_CODE_FINGERPRINT is None:
            _STARTUP_CODE_FINGERPRINT = _code_fingerprint()
        return _STARTUP_CODE_FINGERPRINT


def _registration_stale(reg: dict[str, Any]) -> bool:
    """True when the daemon was started from code that has since been replaced.

    Without this, a singleton daemon silently outlives every reinstall and
    keeps serving stale code to all attached hosts.
    """
    current = _code_fingerprint()
    recorded = reg.get("code_fingerprint")
    return current is not None and recorded is not None and recorded != current


def _terminate_daemon(reg: dict[str, Any], *, timeout: float = 5.0) -> None:
    """SIGTERM the registered daemon pid and wait (bounded) for it to exit."""
    pid = reg.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)


def ensure_daemon(
    workspace: str,
    root: Path | None = None,
    *,
    idle_grace_seconds: float | None = None,
) -> dict[str, Any]:
    """Return a live registration for *workspace*, spawning the daemon if needed.

    Idempotent + race-safe: a fast unlocked check handles the common
    already-running case; on a miss the spawn happens under a per-workspace file
    lock with a second check inside, so concurrent bridges converge on one
    daemon. A healthy daemon running code older than the current install is
    restarted so a reinstall actually takes effect for every host.
    """
    root = default_store_root() if root is None else Path(root)
    ws_hash = workspace_hash(workspace)

    reg = read_daemon_registration(root, ws_hash)
    if reg is not None and _probe_healthy(reg) and not _registration_stale(reg):
        return reg

    _daemon_dir(root).mkdir(parents=True, exist_ok=True)
    with _FileLock(_daemon_lock_path(root, ws_hash)):
        reg = read_daemon_registration(root, ws_hash)
        if reg is not None and _probe_healthy(reg):
            if not _registration_stale(reg):
                return reg
            _terminate_daemon(reg)
        return _spawn_daemon(workspace, root, ws_hash, idle_grace_seconds)


def _spawn_daemon(
    workspace: str,
    root: Path,
    ws_hash: str,
    idle_grace_seconds: float | None,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["LEMONCROW_ROOT"] = str(root)
    # Bind the daemon to exactly one workspace: with per-workspace daemons the
    # process-global workspace env stays correct for every ``_workspace_root()``
    # read, so no per-request workspace threading is needed.
    env["LEMONCROW_WORKSPACE_ROOT"] = workspace
    env["CLAUDE_WORKSPACE_ROOT"] = workspace
    command = [
        sys.executable,
        "-m",
        "lemoncrow.gateway.cli",
        "--root",
        str(root),
        "mcp",
        "daemon",
        "--workspace",
        workspace,
    ]
    if idle_grace_seconds is not None:
        command += ["--idle-grace-seconds", str(idle_grace_seconds)]
    with _daemon_log_path(root, ws_hash).open("a", encoding="utf-8") as log_file:
        subprocess.Popen(  # fixed argv, no shell
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.monotonic() + _SPAWN_HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        reg = read_daemon_registration(root, ws_hash)
        if reg is not None and _probe_healthy(reg):
            return reg
        time.sleep(0.1)
    raise RuntimeError(
        f"MCP daemon for {workspace!r} did not become healthy within "
        f"{_SPAWN_HEALTH_TIMEOUT_SECONDS:.0f}s (see {_daemon_log_path(root, ws_hash)})"
    )


# ── daemon-side activity tracking ────────────────────────────────────────────


class _ActivityTracker:
    """Monotonic last-seen clock for tool traffic (health/observability only)."""

    def __init__(self) -> None:
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def touch(self) -> None:
        with self._lock:
            self._last = time.monotonic()

    def idle_seconds(self) -> float:
        with self._lock:
            return time.monotonic() - self._last


class _LiveSessions:
    """Attached-bridge liveness registry driving idle self-reap.

    Each bridge pings with its own id; a bridge that dies without a clean
    ``/session/close`` drops out once its last ping ages past the TTL. The daemon
    reaps only when zero bridges remain attached, so an open-but-idle session
    (user thinking) is never torn down mid-flight -- only a repo with no live
    sessions is reclaimed.
    """

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def touch(self, bridge_id: str) -> bool:
        """Record a liveness ping; return True the first time *bridge_id* is seen."""
        if not bridge_id:
            return False
        with self._lock:
            is_new = bridge_id not in self._seen
            self._seen[bridge_id] = time.monotonic()
            return is_new

    def drop(self, bridge_id: str) -> None:
        if not bridge_id:
            return
        with self._lock:
            self._seen.pop(bridge_id, None)

    def count(self, ttl: float) -> int:
        cutoff = time.monotonic() - ttl
        with self._lock:
            for bridge_id in [b for b, seen in self._seen.items() if seen < cutoff]:
                self._seen.pop(bridge_id, None)
            return len(self._seen)


# ── daemon runner (server side) ──────────────────────────────────────────────


def run_daemon(
    workspace: str,
    root: Path | None = None,
    *,
    idle_grace_seconds: float = _DEFAULT_IDLE_GRACE_SECONDS,
) -> None:
    """Run the per-workspace singleton daemon (blocks until shutdown).

    Spawned detached by :func:`ensure_daemon`; also runnable directly via the
    hidden ``lc mcp daemon`` command for debugging.
    """
    import uvicorn
    from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

    from lemoncrow.gateway.adapters import mcp_http, mcp_server

    root = default_store_root() if root is None else Path(root)
    os.environ["LEMONCROW_ROOT"] = str(root)
    # This daemon serves exactly one workspace. Pin EVERY workspace variable, not
    # the one or two that happen to be consulted today -- an unpinned var either
    # outranks the pin or shadows it depending on lookup order, which is how the
    # same defect recurred three times under different names.
    pin_workspace_env(workspace)
    ws_hash = workspace_hash(workspace)

    mcp_server._setup_file_logging(str(root))

    # Listen on a per-workspace Unix domain socket instead of a TCP port: same-host
    # bridge<->daemon IPC that no HTTP proxy can hijack (HTTP_PROXY et al. apply
    # only to host:port URLs), needs no ephemeral port, and is guarded by 0600 file
    # perms. Bind up front (unlistened; asyncio calls listen()) after clearing any
    # stale socket a crashed predecessor left behind.
    _daemon_dir(root).mkdir(parents=True, exist_ok=True)
    sock_path = _daemon_socket_path(root, ws_hash)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(sock_path.parent, 0o700)  # per-user socket dir

    # run_daemon() is also reachable directly via the hidden ``lc mcp daemon``
    # command (bypassing ensure_daemon's lock + health-probe entirely), and a
    # respawn can otherwise race a predecessor that is still healthy. Re-check
    # under a dedicated startup lock (NOT ensure_daemon's spawn lock -- see
    # ``_daemon_startup_lock_path``), and claim the registration before
    # releasing it, so at most one live daemon ever owns this workspace's
    # socket regardless of how this process was launched -- instead of
    # unconditionally stealing the socket path out from under it.
    with _FileLock(_daemon_startup_lock_path(root, ws_hash)):
        existing = read_daemon_registration(root, ws_hash)
        if existing is not None and _probe_healthy(existing):
            _log.info(
                "MCP daemon: workspace %s already served by pid=%s; exiting instead of stealing the socket",
                workspace,
                existing.get("pid"),
            )
            return
        with contextlib.suppress(FileNotFoundError):
            sock_path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sock_path))
        with contextlib.suppress(OSError):
            os.chmod(sock_path, 0o600)
        # Identity of the socket THIS process bound, so teardown can tell "my
        # socket" from "the path a rival daemon has since rebound" (see
        # _unlink_owned_socket).
        bound_socket = _socket_identity(sock_path)
        token = secrets.token_urlsafe(32)
        _write_registration(root, ws_hash, socket_path=str(sock_path), token=token, workspace=workspace)

    activity = _ActivityTracker()
    live = _LiveSessions()

    def _verify_token(authorization: str = Header(default="")) -> None:
        scheme, _, presented = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(presented.strip(), token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid daemon token")

    app = FastAPI(
        title="LemonCrow MCP daemon",
        version=mcp_server.SERVER_VERSION,
        description=f"Per-workspace singleton MCP daemon for {workspace}",
    )

    @app.get(_HEALTHZ_PATH)
    async def _healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "workspace": workspace,
            "pid": os.getpid(),
            "idle_seconds": activity.idle_seconds(),
            "live_sessions": live.count(_SESSION_TTL_SECONDS),
        }

    @app.post(_SESSION_PING_PATH, dependencies=[Depends(_verify_token)])
    async def _session_ping(x_lemoncrow_bridge: str = Header(default="")) -> dict[str, Any]:
        if live.touch(x_lemoncrow_bridge):
            _log.info("session attached: bridge=%s", x_lemoncrow_bridge[:8] or "?")
        return {"ok": True}

    @app.post(_SESSION_CLOSE_PATH, dependencies=[Depends(_verify_token)])
    async def _session_close(x_lemoncrow_bridge: str = Header(default="")) -> dict[str, Any]:
        # Prompt reap when a host closes: drop this bridge immediately rather than
        # waiting out the TTL, and terminate the foreground bash it launched
        # (explicit bg=true jobs are preserved, matching stdio shutdown).
        live.drop(x_lemoncrow_bridge)
        if x_lemoncrow_bridge:
            _log.info("session detached: bridge=%s", x_lemoncrow_bridge[:8])
            with contextlib.suppress(Exception):
                from lemoncrow.pro.capabilities.tool_supervision.bash_exec import cleanup_commands_for_owner

                summary = cleanup_commands_for_owner(x_lemoncrow_bridge)
                if summary["terminated"]:
                    _log.info(
                        "session close: terminated %d foreground bash command(s) for bridge %s",
                        len(summary["terminated"]),
                        x_lemoncrow_bridge[:8],
                    )
        return {"ok": True}

    @app.middleware("http")
    async def _track_activity(request: Request, call_next: Any) -> Any:
        # A tool call is also a liveness signal for its bridge, so active work
        # keeps the session attached even if a ping is briefly delayed.
        if request.url.path == mcp_http.MCP_HTTP_PATH:
            activity.touch()
            live.touch(request.headers.get(_BRIDGE_HEADER, ""))
        return await call_next(request)

    mcp_http.register_mcp_http(app, auth_dependency=_verify_token)

    max_concurrency = max(1, int(os.environ.get("LEMONCROW_MCP_DAEMON_MAX_CONCURRENCY", "64")))
    config = uvicorn.Config(
        app,
        log_level="warning",
        limit_concurrency=max_concurrency,
        timeout_keep_alive=30,
    )
    server = uvicorn.Server(config)

    _write_registration(root, ws_hash, socket_path=str(sock_path), token=token, workspace=workspace)
    _warm_daemon(mcp_server)
    stop = threading.Event()
    _start_heartbeat(root, ws_hash, socket_path=str(sock_path), token=token, workspace=workspace, stop=stop)
    _start_idle_reaper(server, live, idle_grace_seconds=idle_grace_seconds, stop=stop)

    _log.info("MCP daemon started: pid=%d workspace=%s socket=%s", os.getpid(), workspace, sock_path)
    exit_code = 0
    try:
        server.run(sockets=[sock])
    except (KeyboardInterrupt, SystemExit) as exc:
        # Graceful signal-driven shutdown, not a crash. On SIGINT/SIGTERM uvicorn
        # stops serving, then its capture_signals re-raises the signal on exit;
        # the CLI's own handler (app._handler) turns that into KeyboardInterrupt.
        # SystemExit covers a direct sys.exit(). Neither warrants a crash log.
        exit_code = exc.code if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 0
        _log.info("MCP daemon shutting down: pid=%d signal-driven exit_code=%d", os.getpid(), exit_code)
    except BaseException:
        _log.exception("MCP daemon crashed")
        exit_code = 1
    finally:
        stop.set()
        # Ownership-scoped teardown: a daemon that lost this workspace (two ever
        # raced for it) must not delete the winner's state. Read ownership BEFORE
        # cleanup, which removes our own registration.
        still_ours = _registration_owner(daemon_registration_path(root, ws_hash)) == os.getpid()
        _shutdown_cleanup(root, ws_hash, mcp_server)
        _unlink_owned_socket(sock_path, bound_socket, still_registered=still_ours)
    _log.info("MCP daemon stopped: pid=%d exit_code=%d", os.getpid(), exit_code)
    # uvicorn / anyio / OTel can leave non-daemon threads that would keep the
    # interpreter resident after an idle self-reap, so the process must never
    # rely on a clean interpreter shutdown. Force-terminate now that cleanup has
    # run (or timed out); the zoekt webserver child is reaped by the kernel via
    # PR_SET_PDEATHSIG, so skipping atexit here is safe.
    os._exit(exit_code)


def _shutdown_cleanup(root: Path, ws_hash: str, mcp_server: Any) -> None:
    """Best-effort teardown, bounded so a stuck exporter flush can't wedge exit.

    Registration removal is attempted first (and is a no-op unless this process
    still owns the file -- see :func:`_remove_registration`); the telemetry
    flushes are allowed to run but are abandoned (daemon thread) if they exceed
    the deadline, after which the caller force-terminates.
    """
    _remove_registration(root, ws_hash)

    def _flush() -> None:
        with contextlib.suppress(Exception):
            mcp_server._emit_mcp_session_end()
        with contextlib.suppress(Exception):
            from lemoncrow.core.service.telemetry import shutdown_otel

            shutdown_otel()

    worker = threading.Thread(target=_flush, daemon=True, name="mcp-daemon-shutdown")
    worker.start()
    worker.join(timeout=5.0)


def _warm_daemon(mcp_server: Any) -> None:
    """Kick the same one-time warmups the stdio server runs, off the hot path."""
    for target in (
        mcp_server._warm_stdio_code_index,
        mcp_server._warm_stdio_embedder,
        mcp_server._warm_stdio_zoekt_webserver,
        mcp_server._auto_init_workspace,
    ):
        threading.Thread(target=target, daemon=True).start()


def _start_idle_reaper(
    server: Any,
    live: _LiveSessions,
    *,
    idle_grace_seconds: float,
    stop: threading.Event,
) -> None:
    # 0/negative disables idle self-reap (daemon lives until signalled), but
    # the loop still runs for the stale-code check below.
    idle_reap = idle_grace_seconds > 0

    def _loop() -> None:
        interval = max(5.0, min(30.0, idle_grace_seconds / 2)) if idle_reap else 30.0
        zero_since: float | None = None
        stale_since: float | None = None
        while not stop.wait(interval):
            # A reinstall replaced the code on disk: exit so the bridge's
            # transparent respawn brings up a daemon running the new code.
            # Without this, a long-lived bridge never rechecks a healthy
            # daemon and every attached host keeps stale behavior.
            current = _code_fingerprint()
            startup = _startup_code_fingerprint()
            if current is not None and startup is not None and current != startup:
                if stale_since is None:
                    stale_since = time.monotonic()
                    _log.info("MCP daemon: installed code changed on disk; restarting once idle")
                attached = live.count(_SESSION_TTL_SECONDS)
                waited = time.monotonic() - stale_since
                if attached > 0 and waited < _STALE_CODE_DRAIN_SECONDS:
                    # Live bridges are mid-conversation. Killing them now costs
                    # far more than running stale code a little longer.
                    continue
                if attached > 0:
                    _log.warning(
                        "MCP daemon: %d session(s) still attached after %.0fs of stale code; restarting anyway",
                        attached,
                        waited,
                    )
                _log.info("MCP daemon: restarting to pick up installed code")
                server.should_exit = True
                # Skip uvicorn's graceful-drain wait: a lingering keep-alive
                # connection must not pin a stale-code daemon alive.
                server.force_exit = True
                # Interpreter shutdown can hang on lingering non-daemon
                # threads (warmups, telemetry). A stale-code daemon must
                # actually die so the bridge respawns new code -- give
                # uvicorn a short drain, then exit hard.
                time.sleep(10.0)
                os._exit(0)
            else:
                # Fingerprint matches again (restored mtime / reverted install):
                # drop the pending restart instead of carrying a stale deadline.
                stale_since = None
            if not idle_reap:
                continue
            if live.count(_SESSION_TTL_SECONDS) == 0:
                # No bridge attached: start (or continue) the grace countdown.
                if zero_since is None:
                    zero_since = time.monotonic()
                elif time.monotonic() - zero_since >= idle_grace_seconds:
                    _log.info("MCP daemon: no attached sessions for %.0fs; shutting down", idle_grace_seconds)
                    server.should_exit = True
                    return
            else:
                zero_since = None

    threading.Thread(target=_loop, daemon=True, name="mcp-daemon-reaper").start()


def _start_heartbeat(
    root: Path,
    ws_hash: str,
    *,
    socket_path: str,
    token: str,
    workspace: str,
    stop: threading.Event,
) -> None:
    """Refresh this daemon's registration until *stop*, or until it loses it.

    Ownership-scoped: see the stand-down guard below.
    """

    def _loop() -> None:
        path = daemon_registration_path(root, ws_hash)
        while not stop.wait(_HEARTBEAT_INTERVAL_SECONDS):
            if _write_registration(
                root,
                ws_hash,
                socket_path=socket_path,
                token=token,
                workspace=workspace,
                only_if_owner=True,
            ):
                continue
            # Declined: the file names a different, still-live daemon. When two
            # daemons served one workspace, both heartbeats kept re-stamping this
            # shared file with their OWN bearer token, so whichever did not own
            # the socket poisoned the token and every authed POST /mcp got 403.
            # Losing the file means losing the race -- log once and stand down
            # rather than fighting over the credential.
            _log.warning(
                "MCP daemon: registration for workspace %s now owned by pid=%s; standing down heartbeat (pid=%d)",
                workspace,
                _registration_owner(path),
                os.getpid(),
            )
            return

    threading.Thread(target=_loop, daemon=True, name="mcp-daemon-heartbeat").start()


# ── registration file I/O ────────────────────────────────────────────────────


def _write_registration(
    root: Path,
    ws_hash: str,
    *,
    socket_path: str,
    token: str,
    workspace: str,
    only_if_owner: bool = False,
) -> bool:
    """Publish this daemon's registration; return False if the write was declined.

    ``only_if_owner`` (heartbeat path) declines when the file already names a
    different, still-live pid -- overwriting it would replace that daemon's
    bearer token with ours. A transient write error still returns ``True``: the
    file is ours, so the next heartbeat simply retries.
    """
    from lemoncrow.gateway.adapters import mcp_server

    path = daemon_registration_path(root, ws_hash)
    if only_if_owner:
        owner = _registration_owner(path)
        if owner is not None and owner != os.getpid():
            return False
    payload = {
        "pid": os.getpid(),
        "socket": socket_path,
        "token": token,
        "workspace": workspace,
        "ws_hash": ws_hash,
        "version": mcp_server.SERVER_VERSION,
        # Code identity at daemon start: lets bridges detect a daemon that
        # outlived a reinstall and restart it (see _registration_stale).
        "code_fingerprint": _startup_code_fingerprint(),
        # Preserve the original start time across heartbeat rewrites.
        "started_at": _existing_started_at(path),
        "last_heartbeat": time.time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        # Token is a bearer credential: keep the file owner-only.
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError:
        _log.debug("MCP daemon registration write failed", exc_info=True)
    return True


def _existing_started_at(path: Path) -> float:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("started_at"), (int, float)):
            return float(data["started_at"])
    except (OSError, ValueError):
        pass
    return time.time()


def _remove_registration(root: Path, ws_hash: str) -> None:
    """Remove the registration -- but only while it still names THIS process.

    Two daemons can briefly coexist for one workspace (a direct ``lc mcp daemon``
    racing a bridge respawn). The loser exiting used to delete the winner's
    registration on its way out, leaving every bridge with nothing to dial.
    Never delete another daemon's state.
    """
    if _registration_owner(daemon_registration_path(root, ws_hash)) != os.getpid():
        return
    with contextlib.suppress(OSError):
        daemon_registration_path(root, ws_hash).unlink(missing_ok=True)


# ── introspection (used by ``lc mcp daemons`` / servicectl prune later) ───────


def list_daemons(root: Path | None = None) -> list[dict[str, Any]]:
    """Live daemon registrations (dead-pid entries skipped), oldest first."""
    root = default_store_root() if root is None else Path(root)
    directory = _daemon_dir(root)
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(directory.glob("*.json")):
        ws_hash = entry.stem
        reg = read_daemon_registration(root, ws_hash)
        if reg is not None:
            reg["registration_file"] = str(entry)
            out.append(reg)
    out.sort(key=lambda r: r.get("started_at", 0.0))
    return out


def prune_stale_daemons(root: Path | None = None) -> int:
    """Delete registration files whose daemon pid is dead; return the count.

    A daemon removes its own registration on clean exit, so this only ever fires
    on a crash-without-cleanup. Called from the servicectl maintenance tick as a
    cheap glob so stale files never accumulate under ``mcp_daemons/``.
    """
    root = default_store_root() if root is None else Path(root)
    directory = _daemon_dir(root)
    if not directory.is_dir():
        return 0
    removed = 0
    for entry in sorted(directory.glob("*.json")):
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        pid = data.get("pid") if isinstance(data, dict) else None
        if not isinstance(pid, int) or not _pid_alive(pid):
            sock = data.get("socket") if isinstance(data, dict) else None
            with contextlib.suppress(OSError):
                entry.unlink()
                removed += 1
            # A dead pid licenses removing its registration, but NOT the socket:
            # the path is deterministic per workspace, so the live successor that
            # replaced this daemon is already bound to the very path the dead one
            # registered. Unlinking it there strands every bridge for the
            # workspace on a path nothing listens to, while the daemon keeps
            # serving a socket no one can reach. Unlink only a proven orphan.
            if isinstance(sock, str) and sock and not _socket_has_listener(sock):
                with contextlib.suppress(OSError):
                    Path(sock).unlink()
    return removed


__all__ = [
    "daemon_registration_path",
    "ensure_daemon",
    "list_daemons",
    "prune_stale_daemons",
    "read_daemon_registration",
    "run_daemon",
]
