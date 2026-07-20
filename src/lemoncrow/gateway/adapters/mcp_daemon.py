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
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.paths import default_store_root
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
# A bridge that stops pinging for longer than this is treated as detached (covers
# an ungraceful bridge death that never sent /session/close). Must exceed the
# bridge ping interval (see mcp_bridge._PING_INTERVAL_SECONDS) with margin.
_SESSION_TTL_SECONDS = 90.0
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


def _daemon_log_path(root: Path, ws_hash: str) -> Path:
    return _daemon_dir(root) / f"{ws_hash}.log"


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
    port = data.get("port")
    if not isinstance(port, int) or port <= 0:
        return None
    return data


def _base_url(reg: dict[str, Any]) -> str:
    return f"http://127.0.0.1:{int(reg['port'])}"


def _probe_healthy(reg: dict[str, Any], *, timeout: float = 2.0) -> bool:
    """True if the daemon answers its health route.

    Liveness only (the registration's pid + a listenable port); token
    correctness is guaranteed because the bridge reads the token from the same
    file the daemon wrote, so the health route stays unauthenticated + cheap.
    """
    import httpx

    try:
        resp = httpx.get(_base_url(reg) + _HEALTHZ_PATH, timeout=timeout)
    except Exception:
        return False
    return resp.status_code == 200


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
    daemon.
    """
    root = default_store_root() if root is None else Path(root)
    ws_hash = workspace_hash(workspace)

    reg = read_daemon_registration(root, ws_hash)
    if reg is not None and _probe_healthy(reg):
        return reg

    _daemon_dir(root).mkdir(parents=True, exist_ok=True)
    with _FileLock(_daemon_lock_path(root, ws_hash)):
        reg = read_daemon_registration(root, ws_hash)
        if reg is not None and _probe_healthy(reg):
            return reg
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

    def touch(self, bridge_id: str) -> None:
        if not bridge_id:
            return
        with self._lock:
            self._seen[bridge_id] = time.monotonic()

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
    os.environ["LEMONCROW_WORKSPACE_ROOT"] = workspace
    os.environ.setdefault("CLAUDE_WORKSPACE_ROOT", workspace)
    ws_hash = workspace_hash(workspace)

    mcp_server._setup_file_logging(str(root))

    # Bind the loopback socket up front so the port is known before we publish
    # the registration; hand it to uvicorn unlistened (asyncio calls listen()).
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    token = secrets.token_urlsafe(32)

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
        live.touch(x_lemoncrow_bridge)
        return {"ok": True}

    @app.post(_SESSION_CLOSE_PATH, dependencies=[Depends(_verify_token)])
    async def _session_close(x_lemoncrow_bridge: str = Header(default="")) -> dict[str, Any]:
        # Prompt reap when a host closes: drop this bridge immediately rather than
        # waiting out the TTL, and terminate the foreground bash it launched
        # (explicit bg=true jobs are preserved, matching stdio shutdown).
        live.drop(x_lemoncrow_bridge)
        if x_lemoncrow_bridge:
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

    _write_registration(root, ws_hash, port=port, token=token, workspace=workspace)
    _warm_daemon(mcp_server)
    stop = threading.Event()
    _start_heartbeat(root, ws_hash, port=port, token=token, workspace=workspace, stop=stop)
    _start_idle_reaper(server, live, idle_grace_seconds=idle_grace_seconds, stop=stop)

    exit_code = 0
    try:
        server.run(sockets=[sock])
    except SystemExit as exc:  # signal-driven shutdown (SIGTERM/SIGHUP)
        exit_code = exc.code if isinstance(exc.code, int) else 0
    except BaseException:
        _log.exception("MCP daemon crashed")
        exit_code = 1
    finally:
        stop.set()
        _shutdown_cleanup(root, ws_hash, mcp_server)
    # uvicorn / anyio / OTel can leave non-daemon threads that would keep the
    # interpreter resident after an idle self-reap, so the process must never
    # rely on a clean interpreter shutdown. Force-terminate now that cleanup has
    # run (or timed out); the zoekt webserver child is reaped by the kernel via
    # PR_SET_PDEATHSIG, so skipping atexit here is safe.
    os._exit(exit_code)


def _shutdown_cleanup(root: Path, ws_hash: str, mcp_server: Any) -> None:
    """Best-effort teardown, bounded so a stuck exporter flush can't wedge exit.

    Registration removal must always happen; the telemetry flushes are allowed to
    run but are abandoned (daemon thread) if they exceed the deadline, after
    which the caller force-terminates.
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
    if idle_grace_seconds <= 0:
        return  # 0/negative disables self-reap (daemon lives until signalled).

    def _loop() -> None:
        interval = max(5.0, min(30.0, idle_grace_seconds / 2))
        zero_since: float | None = None
        while not stop.wait(interval):
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
    port: int,
    token: str,
    workspace: str,
    stop: threading.Event,
) -> None:
    def _loop() -> None:
        while not stop.wait(_HEARTBEAT_INTERVAL_SECONDS):
            _write_registration(root, ws_hash, port=port, token=token, workspace=workspace)

    threading.Thread(target=_loop, daemon=True, name="mcp-daemon-heartbeat").start()


# ── registration file I/O ────────────────────────────────────────────────────


def _write_registration(
    root: Path,
    ws_hash: str,
    *,
    port: int,
    token: str,
    workspace: str,
) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    path = daemon_registration_path(root, ws_hash)
    payload = {
        "pid": os.getpid(),
        "port": port,
        "token": token,
        "workspace": workspace,
        "ws_hash": ws_hash,
        "version": mcp_server.SERVER_VERSION,
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


def _existing_started_at(path: Path) -> float:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("started_at"), (int, float)):
            return float(data["started_at"])
    except (OSError, ValueError):
        pass
    return time.time()


def _remove_registration(root: Path, ws_hash: str) -> None:
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
            with contextlib.suppress(OSError):
                entry.unlink()
                removed += 1
    return removed


__all__ = [
    "daemon_registration_path",
    "ensure_daemon",
    "list_daemons",
    "prune_stale_daemons",
    "read_daemon_registration",
    "run_daemon",
]
