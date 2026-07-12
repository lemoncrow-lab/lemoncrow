"""Managed Zoekt runtime for large-repo text search routing."""

from __future__ import annotations

import atexit
import base64
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from lemoncrow.core.foundation.paths import default_store_root

from .binary import ZoektBinaryResolution, discover_zoekt_binary

_BRIDGE_SENTINEL = "__LEMONCROW_ZOEKT_END__"
_DOCKER_NOFILE = "1048576:1048576"
_STARTUP_TIMEOUT_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 0.25
# Background readiness deadline: big indexes (e.g. astropy, seaborn) mmap their
# shards slowly, so give the poll thread enough time to see a loaded repo before
# giving up and marking the server failed.
_WEBSERVER_READY_TIMEOUT_SECONDS = 30.0
_WEBSERVER_READY_POLL_SECONDS = 0.05


# Per-request Zoekt timeout (HTTP webserver + CLI subprocess).
# 200 ms covers the vast majority of queries (typical: 5-50 ms) while
# bounding tail latency from complex regex patterns.  Override via
# LEMONCROW_ZOEKT_REQUEST_TIMEOUT_MS.
def _zoekt_request_timeout() -> float:
    raw = os.environ.get("LEMONCROW_ZOEKT_REQUEST_TIMEOUT_MS")
    if raw:
        try:
            return max(0.010, float(raw) / 1000.0)
        except ValueError:
            pass
    return 0.200


_WEBSERVER_REQUEST_TIMEOUT_SECONDS = _zoekt_request_timeout()
_SKIP_ROOTS = {".git", ".jj", ".lemoncrow", ".venv", "node_modules", "dist", "build", "__pycache__"}


def _set_pdeathsig() -> None:
    """Ensure the child process receives SIGTERM if this parent dies.

    Uses Linux ``prctl(PR_SET_PDEATHSIG)`` so even ``SIGKILL`` of the
    parent propagates — ``atexit`` alone is unreliable for subprocess
    lifecycle.
    """
    import ctypes
    import ctypes.util

    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1  # linux/prctl.h
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except (OSError, AttributeError):
        # ctypes raises OSError when the C library can't be loaded; there is no
        # ctypes.CDLLError. AttributeError covers a missing prctl symbol.
        pass


def _get_pgid_safely(pid: int) -> int | None:
    """Return the process group ID for *pid*, or None if the process is gone."""
    try:
        return os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return None


@dataclass(frozen=True)
class ZoektHealth:
    ok: bool
    backend: str
    binary_path: str | None
    index_age_seconds: int | None


class ZoektServer:
    """Shared Zoekt runtime with session-scoped lifecycle reuse."""

    def __init__(self, repo_root: Path, *, resolution: ZoektBinaryResolution | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.resolution = resolution
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._bridge: subprocess.Popen[str] | None = None
        self._container_id: str | None = None
        self._host_search_binary: Path | None = None
        # Persistent host-mode zoekt-webserver: started lazily, reused across
        # queries, torn down in stop().  Guarded by _webserver_lock so a
        # concurrent first query does not race two server launches.
        self._webserver_lock = threading.Lock()
        self._webserver_proc: subprocess.Popen[bytes] | None = None
        self._webserver_url: str | None = None
        # PID of the process that spawned the live webserver.  A forked child
        # (e.g. a benchmark worker) inherits the handle but must not poll or
        # kill a server it does not own -- it queries the inherited URL over
        # HTTP instead.
        self._webserver_owner_pid: int | None = None
        self._webserver_failed = False
        # Set once the webserver is actually queryable; waiters block on this
        # Event instead of holding _webserver_lock so startup never blocks a
        # concurrent tool call.
        self._webserver_ready: threading.Event = threading.Event()
        from lemoncrow.core.foundation.paths import workspace_key

        self._container_name = f"lemoncrow-zoekt-{workspace_key(self.repo_root)[:40]}-{os.getpid()}"
        self._started_at: float | None = None
        self.start_count = 0

    @property
    def runtime_root(self) -> Path:
        from lemoncrow.core.foundation.paths import workspace_key

        workspace_hash = workspace_key(self.repo_root)
        return default_store_root() / "workspaces" / workspace_hash / "zoekt"

    @property
    def index_root(self) -> Path:
        return self.runtime_root / "index"

    @property
    def state_path(self) -> Path:
        return self.runtime_root / "state.json"

    @property
    def input_root(self) -> Path:
        return self.runtime_root / "input"

    def ensure_started(self) -> str:
        """Register this workspace against an existing Zoekt index.

        Only wires up the binary handle and returns.  Never builds or
        rebuilds the index -- that is ``build_index()``'s job, called
        offline from ``lc code index`` / ``lc zoekt up``.
        Raises ``RuntimeError`` if no index is available so the caller
        can degrade gracefully instead of paying an inline build cost.
        """
        with self._lock:
            if self._is_ready():
                return self.handle
            resolution = self.resolution or discover_zoekt_binary(self.repo_root)
            if not resolution.available:
                raise RuntimeError(resolution.reason or "zoekt runtime unavailable")
            self.resolution = resolution
            if resolution.runtime == "docker":
                # Docker runtime must be started (container launch is fast).
                self._start_docker_runtime(resolution)
            else:
                # Host binary mode: register against the on-disk index.  The
                # _is_ready() check at the top returned False *before* the
                # resolution was set (its first guard is `resolution is None`),
                # so re-check now that the binary is resolved -- otherwise a
                # perfectly good on-disk index (state.json + shards present) is
                # mis-reported as missing on the very first call, which raises
                # and kills the whole zoekt path.
                if self._is_ready():
                    self.start_count += 1
                    return self.handle
                raise RuntimeError(
                    f"no Zoekt index found at {self.index_root} -- "
                    "run 'lc code index' or 'lc zoekt up' to build it first"
                )
            self.start_count += 1
            return self.handle

    def ensure_started_and_build(self) -> str:
        """Start Zoekt, building the index if missing.  For indexing routes only."""
        with self._lock:
            if self._is_ready():
                return self.handle
            resolution = self.resolution or discover_zoekt_binary(self.repo_root)
            if not resolution.available:
                raise RuntimeError(resolution.reason or "zoekt runtime unavailable")
            self.resolution = resolution
            if resolution.runtime == "docker":
                self._start_docker_runtime(resolution)
            else:
                self.build_index(resolution)
                self._started_at = self._load_started_at()
            self.start_count += 1
            return self.handle

    @property
    def handle(self) -> str:
        if self.resolution is None:
            raise RuntimeError("Zoekt runtime has not been started")
        if self.resolution.runtime == "docker":
            if not self._container_id:
                raise RuntimeError("Zoekt container has not been started")
            return f"docker://{self._container_id}"
        return f"binary://{self.index_root}"

    def health(self) -> ZoektHealth:
        if not self._is_ready():
            runtime_ref = None
            if self.resolution is not None:
                runtime_ref = self.resolution.image_ref or (
                    str(self.resolution.path) if self.resolution.path is not None else None
                )
            return ZoektHealth(
                ok=False,
                backend="zoekt",
                binary_path=runtime_ref,
                index_age_seconds=None,
            )
        runtime_ref = None
        if self.resolution is not None:
            runtime_ref = self.resolution.image_ref or (
                str(self.resolution.path) if self.resolution.path is not None else None
            )
        index_age_seconds = None
        if self._started_at is not None:
            index_age_seconds = int(max(0, time.time() - self._started_at))
        return ZoektHealth(
            ok=True,
            backend="zoekt",
            binary_path=runtime_ref,
            index_age_seconds=index_age_seconds,
        )

    def raw_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_started()
        if self.resolution is None:
            raise RuntimeError("Zoekt runtime has not been resolved")
        if self.resolution.runtime == "docker":
            return self._bridge_request(payload)
        return self._host_search(payload)

    def stop(self) -> None:
        with self._lock:
            if self._bridge is not None:
                bridge = self._bridge
                self._bridge = None
                with suppress(Exception):
                    if bridge.stdin is not None:
                        bridge.stdin.close()
                with suppress(Exception):
                    bridge.terminate()
                try:
                    bridge.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # SIGTERM did not land; escalate to SIGKILL and reap so the
                    # docker exec child is not left defunct.
                    with suppress(Exception):
                        bridge.kill()
                    with suppress(Exception):
                        bridge.wait(timeout=5)
            if self._container_id is not None:
                container_id = self._container_id
                self._container_id = None
                _run_command(["docker", "stop", container_id], check=False, timeout=30)
            with self._webserver_lock:
                self._stop_webserver()
            self._host_search_binary = None
            self._started_at = None

    def _is_ready(self) -> bool:
        if self.resolution is None:
            return False
        if self.resolution.runtime == "docker":
            return self._container_id is not None and self._bridge is not None and self._bridge.poll() is None
        # Host binary mode: check on-disk state so a prior-process prewarm
        # (entry-script or `lc code index`) survives MCP server restart
        # without a full rebuild.  The in-process _host_search_binary pointer
        # is lazily restored from the resolution if disk state is present.
        if not self.state_path.exists() or not any(self.index_root.glob("*.zoekt")):
            return False
        if self._host_search_binary is None and self.resolution is not None:
            with suppress(Exception):
                self._host_search_binary, *_ = _resolve_host_binaries(self.resolution)
        return self._host_search_binary is not None

    def _start_docker_runtime(self, resolution: ZoektBinaryResolution) -> None:
        if not resolution.image_ref:
            raise RuntimeError("managed docker runtime is missing an image reference")
        self._prepare_runtime_dirs()
        self._refresh_input_links()
        inspect = _run_command(["docker", "image", "inspect", resolution.image_ref], check=False, timeout=30)
        if inspect.returncode != 0:
            _run_command(["docker", "pull", resolution.image_ref], timeout=300)
        # A previous bridge timeout kills the bridge Popen but leaves the
        # named container running; remove any leftover so this start is
        # idempotent and `docker run --name` does not collide.
        _run_command(["docker", "rm", "-f", self._container_name], check=False, timeout=30)
        command = (
            "set -eu\n"
            "zoekt-index -index /data/index /input >/dev/null\n"
            'printf \'{"started_at": %s}\\n\' "$(date +%s)" > /data/index/.lemoncrow-zoekt-state.json\n'
            "exec zoekt-webserver -index /data/index -pprof -rpc\n"
        )
        completed = _run_command(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--ulimit",
                f"nofile={_DOCKER_NOFILE}",
                "--name",
                self._container_name,
                "-v",
                f"{self.input_root}:/input:ro",
                "-v",
                f"{self.index_root}:/data/index",
                resolution.image_ref,
                "sh",
                "-lc",
                command,
            ],
            timeout=120,
        )
        self._container_id = completed.stdout.strip()
        self._wait_for_container_ready()
        self._bridge = _start_bridge(self._container_id)
        self._started_at = self._load_started_at()

    def build_index(self, resolution: ZoektBinaryResolution) -> None:
        """Build or incrementally update the Zoekt index for this workspace.

        **Never call this on the MCP tool-call hot path.**  It is the
        indexing route: ``lc code index``, ``lc zoekt up``, and
        the benchmark prewarm script.  MCP search calls go through
        ``ensure_started()`` which only *registers* an existing index.

        For git repos ``zoekt-git-index`` is used: it stores indexed
        git-object hashes in each shard and automatically re-indexes only
        changed objects on subsequent runs, handling deletions correctly
        (no stale shard accumulation).  For non-git directories
        ``zoekt-index`` does a full rebuild.
        """
        # Guard against a degenerate root.  ``_workspace_root()`` falls back to
        # ``os.getcwd()`` when no workspace env var is set, so a server launched
        # with cwd ``/`` would otherwise try to index the entire filesystem.
        if self.repo_root == self.repo_root.parent:
            raise RuntimeError(
                f"refusing to index filesystem root {self.repo_root}: no project root "
                "resolved (set CLAUDE_WORKSPACE_ROOT or run from a project directory)"
            )

        search_binary, index_binary, git_index_binary = _resolve_host_binaries(resolution)
        self._prepare_runtime_dirs()
        self._refresh_input_links()

        is_git = (self.repo_root / ".git").exists()
        if git_index_binary is not None and is_git:
            # Inherently incremental: first run indexes everything; subsequent
            # runs diff against shard metadata and only touch changed objects.
            _run_command(
                [str(git_index_binary), "-index", str(self.index_root), str(self.repo_root)],
                timeout=300,
            )
        else:
            # No git-aware indexer available or non-git dir: full rebuild.
            _run_command(
                [str(index_binary), "-index", str(self.index_root), str(self.input_root)],
                timeout=300,
            )

        state: dict[str, Any] = {"started_at": int(time.time())}
        head = _read_git_head(self.repo_root)
        if head is not None:
            # Stamp the indexed commit so a background refresh can detect HEAD
            # moves (zoekt-git-index is commit-granular -- see build_index docs).
            state["head"] = head
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self._host_search_binary = search_binary

    def _prepare_runtime_dirs(self) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.index_root.mkdir(parents=True, exist_ok=True)
        self.input_root.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.runtime_root.chmod(0o700)
        with suppress(OSError):
            self.index_root.chmod(0o700)
        with suppress(OSError):
            self.input_root.chmod(0o700)

    def _refresh_input_links(self) -> None:
        shutil.rmtree(self.input_root, ignore_errors=True)
        self.input_root.mkdir(parents=True, exist_ok=True)
        for entry in sorted(self.repo_root.iterdir()):
            if entry.name in _SKIP_ROOTS or entry.name.startswith("."):
                continue
            _mirror_entry(entry, self.input_root / entry.name)

    def _wait_for_container_ready(self) -> None:
        if self._container_id is None:
            raise RuntimeError("Zoekt container did not start")
        deadline = time.time() + _STARTUP_TIMEOUT_SECONDS
        while time.time() < deadline:
            probe = _run_command(
                [
                    "docker",
                    "exec",
                    self._container_id,
                    "sh",
                    "-lc",
                    "wget -qO- http://127.0.0.1:6070/healthz >/dev/null || wget -qO- http://127.0.0.1:6070/ >/dev/null",
                ],
                check=False,
                timeout=10,
            )
            if probe.returncode == 0:
                return
            status = _run_command(
                ["docker", "inspect", "-f", "{{.State.Running}}", self._container_id], check=False, timeout=10
            )
            if status.returncode != 0 or status.stdout.strip() != "true":
                logs = _run_command(["docker", "logs", self._container_id], check=False, timeout=10)
                raise RuntimeError(
                    logs.stderr.strip() or logs.stdout.strip() or "zoekt container exited before becoming ready"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)
        raise RuntimeError("zoekt container did not become ready in time")

    def _bridge_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        bridge = self._bridge
        if bridge is None or bridge.stdin is None or bridge.stdout is None:
            raise RuntimeError("zoekt bridge is not running")
        encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
        with self._request_lock:
            bridge.stdin.write(encoded + "\n")
            bridge.stdin.flush()
            # readline() can block forever if Docker or the webserver stalls.
            # A threading.Timer kills the bridge after 30 s, which makes
            # readline() return "" (EOF) so the loop exits cleanly.
            # We can't use select.select() here: Python's TextIOWrapper may
            # have already pulled the sentinel line into its internal buffer
            # from the same OS read as the JSON body, leaving the fd empty
            # and causing a spurious select timeout.
            timed_out = threading.Event()

            def _kill_bridge() -> None:
                timed_out.set()
                with suppress(Exception):
                    bridge.kill()
                # Reap the killed child and drop the handle so it is not left as a
                # zombie with leaked pipe fds, and so _is_ready() reads False
                # unambiguously for the now-dead bridge.
                with suppress(Exception):
                    bridge.wait(timeout=5)
                self._bridge = None

            response_lines: list[str] = []
            timer = threading.Timer(30.0, _kill_bridge)
            timer.start()
            try:
                while True:
                    line = bridge.stdout.readline()
                    if line == "":
                        if timed_out.is_set():
                            raise TimeoutError("zoekt bridge did not respond within 30 s")
                        stderr = ""
                        if bridge.stderr is not None:
                            with suppress(Exception):
                                stderr = bridge.stderr.read().strip()
                        raise RuntimeError(stderr or "zoekt bridge exited unexpectedly")
                    if line.rstrip("\n") == _BRIDGE_SENTINEL:
                        break
                    response_lines.append(line)
            finally:
                timer.cancel()
        body = "".join(response_lines).strip()
        if not body:
            raise RuntimeError("zoekt bridge returned an empty response")
        return cast(dict[str, Any], json.loads(body))

    def _host_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Host-mode search via the persistent ``zoekt-webserver``.

        A single long-lived ``zoekt-webserver`` is started lazily and queried
        over HTTP -- the index stays resident, so each query is single-digit
        ms instead of paying Go-runtime init + index mmap on a fresh ``zoekt``
        subprocess.  The webserver is the *only* host search surface: there is
        no per-query CLI fallback.  When the server cannot be started or does
        not answer, this returns an empty result so the caller degrades to its
        non-zoekt channels rather than spawning a subprocess per query.
        """
        url = self._ensure_webserver()
        if url is None:
            return {"Result": {"Files": []}}
        try:
            return self._run_webserver_search(url, payload)
        except Exception:  # noqa: BLE001 -- HTTP/parse error: drop this query's zoekt results
            # A 4xx (e.g. a malformed-regexp query the API rejects) leaves a
            # perfectly healthy server up, so only tear it down when the
            # process has actually died; otherwise keep serving later queries.
            logging.debug("zoekt webserver search failed", exc_info=True)
            with self._webserver_lock:
                proc = self._webserver_proc
                if proc is None or proc.poll() is not None:
                    self._stop_webserver()
            return {"Result": {"Files": []}}

    def _ensure_webserver(self) -> str | None:
        """Return the base URL of a live host zoekt-webserver, starting one lazily.

        Returns ``None`` (never raises) when the webserver cannot be started
        so the caller falls back to the per-query CLI path.

        Non-blocking design
        -------------------
        The readiness poll (up to ~15 s) runs outside ``_webserver_lock`` so
        a concurrent tool call is never blocked by the startup thread holding
        the mutex.  Instead callers wait on ``_webserver_ready`` (a
        ``threading.Event``) which costs no CPU and is released the moment the
        server is queryable.
        """
        with self._webserver_lock:
            if self._webserver_failed:
                return None
            proc = self._webserver_proc
            inherited = self._webserver_owner_pid is not None and self._webserver_owner_pid != os.getpid()
            if inherited and self._webserver_url is not None:
                # Server was started by another process (the benchmark parent
                # before fork).  Query it over HTTP; never poll/kill its pid --
                # proc.poll() is unreliable for a non-child and stopping it
                # would SIGTERM the parent's shared server.  A genuinely dead
                # server just yields an HTTP error that _host_search degrades on.
                url = self._webserver_url
            elif proc is not None and proc.poll() is None and self._webserver_url is not None:
                # Already started — if not yet ready, wait below outside the lock.
                url = self._webserver_url
            else:
                # A prior server died or first call: clear stale state.
                self._stop_webserver()
                try:
                    url = self._spawn_webserver_process()
                except Exception:  # noqa: BLE001
                    logging.debug("zoekt webserver spawn failed; using CLI fallback", exc_info=True)
                    self._stop_webserver()
                    self._webserver_failed = True
                    return None

        # Non-blocking readiness: the hot query path must never stall.  If the
        # background poll thread hasn't signalled readiness yet, return None so
        # this one call degrades to the non-zoekt channels -- the next call
        # after the Event is set uses the live server.  Startup latency is
        # moved off the hot path by callers that warm up front via
        # ``wait_until_searchable`` (benchmark prewarm / production index warm).
        if not self._webserver_ready.is_set():
            return None
        if self._webserver_failed:
            return None
        return url

    def _spawn_webserver_process(self) -> str:
        """Spawn zoekt-webserver and start a daemon thread that polls readiness.

        Stores ``_webserver_proc`` and ``_webserver_url`` immediately (under
        the caller's lock) then returns the URL.  Readiness is signalled via
        ``_webserver_ready`` once ``_wait_for_webserver_ready`` succeeds.
        Caller must hold ``_webserver_lock``.
        """
        if self.resolution is None:
            raise RuntimeError("zoekt runtime has not been resolved")
        webserver_binary = _resolve_webserver_binary(self.resolution)
        port = _pick_free_port()
        proc = subprocess.Popen(
            [
                str(webserver_binary),
                "-listen",
                f"127.0.0.1:{port}",
                "-index",
                str(self.index_root),
                "-rpc",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=_set_pdeathsig,
        )
        url = f"http://127.0.0.1:{port}"
        self._webserver_proc = proc
        self._webserver_url = url
        self._webserver_owner_pid = os.getpid()
        self._webserver_ready.clear()

        # Readiness poll runs in a daemon thread so the caller's lock is free.
        def _poll_ready() -> None:
            ok = _wait_for_webserver_ready(url, proc)
            if ok:
                self._webserver_ready.set()
            else:
                with self._webserver_lock:
                    self._stop_webserver()
                    self._webserver_failed = True
                self._webserver_ready.set()  # unblock any waiters so they see failed=True

        threading.Thread(target=_poll_ready, daemon=True, name="zoekt-ready-poll").start()
        return url

    def wait_until_searchable(self, timeout: float) -> bool:
        """Block until the host webserver can serve queries, or ``timeout``.

        Triggers a lazy start if needed, then waits for the background
        readiness poll.  Returns ``True`` only when the server is live and a
        repo with loaded documents is queryable.  Callers (benchmark prewarm,
        production index warm) use this to move the one-time startup cost off
        the hot query path so steady-state searches never block.
        """
        deadline = time.time() + timeout
        # Resolve the runtime first (sets self.resolution) so the webserver
        # spawn can find its binary, then trigger a lazy spawn without issuing
        # a real query.
        with suppress(Exception):
            self.ensure_started()
        with suppress(Exception):
            self._ensure_webserver()
        remaining = max(0.0, deadline - time.time())
        self._webserver_ready.wait(timeout=remaining)
        return self._webserver_ready.is_set() and not self._webserver_failed

    def _run_webserver_search(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"Q": str(payload.get("Q") or "")}, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{url}/api/search",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=_WEBSERVER_REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
        return cast(dict[str, Any], json.loads(raw))

    def _stop_webserver(self) -> None:
        """Terminate the host webserver and clear its handles. Caller holds the lock.

        Kills the entire process group (``start_new_session=True`` in
        ``_spawn_webserver_process`` puts the child in its own session, so
        ``os.killpg`` reaches the webserver and any grandchild processes).
        """
        proc = self._webserver_proc
        owner = self._webserver_owner_pid
        self._webserver_proc = None
        self._webserver_url = None
        self._webserver_owner_pid = None
        self._webserver_ready.clear()  # reset so next _spawn waits for fresh readiness
        if proc is None:
            return
        if owner is not None and owner != os.getpid():
            # Inherited from another process; not ours to kill -- just drop the
            # local handle so this process stops referencing it.
            return
        pgid = _get_pgid_safely(proc.pid)
        if pgid is not None:
            with suppress(Exception):
                os.killpg(pgid, signal.SIGTERM)
        else:
            with suppress(Exception):
                proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                with suppress(Exception):
                    os.killpg(pgid, signal.SIGKILL)
            else:
                with suppress(Exception):
                    proc.kill()
            with suppress(Exception):
                proc.wait(timeout=5)

    def _load_started_at(self) -> float | None:
        candidates = [self.index_root / ".lemoncrow-zoekt-state.json", self.state_path]
        for path in candidates:
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            value = payload.get("started_at")
            if isinstance(value, (int, float)):
                return float(value)
        return None

    def index_present(self) -> bool:
        """True when a host-mode shard set exists on disk (no binary needed)."""
        return self.state_path.exists() and any(self.index_root.glob("*.zoekt"))

    def current_git_head(self) -> str | None:
        """Resolved git HEAD of the working repo, or None if not a git repo."""
        return _read_git_head(self.repo_root)

    def indexed_git_head(self) -> str | None:
        """The git HEAD that the on-disk index was last built from, if recorded."""
        for path in (self.index_root / ".lemoncrow-zoekt-state.json", self.state_path):
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            head = payload.get("head")
            if isinstance(head, str) and head:
                return head
        return None


def _read_git_head(repo_root: Path) -> str | None:
    """Resolve a repo's git HEAD to a commit sha via cheap file reads.

    Returns the loose-ref sha after a commit (the common case). Falls back to
    the symbolic ref string when the ref is packed/unborn -- still a stable
    change-detection key. None when the path is not a git repo.
    """
    head_file = repo_root / ".git" / "HEAD"
    try:
        ref = head_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if ref.startswith("ref: "):
        ref_path = repo_root / ".git" / ref[5:]
        try:
            return ref_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ref
    return ref


def _resolve_webserver_binary(resolution: ZoektBinaryResolution) -> Path:
    """Locate ``zoekt-webserver`` beside the pinned ``zoekt`` binary."""
    if resolution.path is None:
        raise RuntimeError("zoekt host runtime is missing the pinned binary path")
    root = resolution.path.parent
    webserver_binary = root / "zoekt-webserver"
    if not webserver_binary.is_file() or not os.access(webserver_binary, os.X_OK):
        raise RuntimeError(f"zoekt-webserver binary is missing beside {resolution.path}")
    return webserver_binary


def _pick_free_port() -> int:
    """Reserve an ephemeral localhost port via bind(:0) and return it."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_webserver_ready(url: str, proc: subprocess.Popen[bytes]) -> bool:
    """Poll until the index is *searchable*, or the process dies / times out.

    ``/healthz`` flips to 200 as soon as the HTTP listener is up -- but the
    index shards finish loading a few ms later, and a search issued in that
    window silently returns zero results.  The authoritative readiness signal
    is ``/api/list`` reporting at least one loaded repository whose shard
    Documents count is non-zero, which only happens once the shards are
    actually mmap'd and queryable.
    """
    deadline = time.time() + _WEBSERVER_READY_TIMEOUT_SECONDS
    body = json.dumps({"Q": ""}, separators=(",", ":")).encode("utf-8")
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            request = urllib.request.Request(
                f"{url}/api/list",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=1.0) as response:
                if response.status == 200 and _list_has_loaded_repo(response.read()):
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(_WEBSERVER_READY_POLL_SECONDS)
    return False


def _list_has_loaded_repo(raw: bytes) -> bool:
    """True when /api/list reports a repo with at least one indexed document."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return False
    repos = ((payload.get("List") or {}) if isinstance(payload, dict) else {}).get("Repos")
    if not isinstance(repos, list):
        return False
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        stats = repo.get("Stats")
        if isinstance(stats, dict) and int(stats.get("Documents") or 0) > 0:
            return True
    return False


def _resolve_host_binaries(resolution: ZoektBinaryResolution) -> tuple[Path, Path, Path | None]:
    """Return (search_binary, plain_index_binary, git_index_binary|None)."""
    if resolution.path is None:
        raise RuntimeError("zoekt host runtime is missing the pinned binary path")
    root = resolution.path.parent
    search_binary = resolution.path if resolution.path.name == "zoekt" else root / "zoekt"
    index_binary = root / "zoekt-index"
    git_index_binary = root / "zoekt-git-index"
    if not search_binary.is_file() or not os.access(search_binary, os.X_OK):
        raise RuntimeError(f"zoekt search binary is missing beside {resolution.path}")
    if not index_binary.is_file() or not os.access(index_binary, os.X_OK):
        raise RuntimeError(f"zoekt-index binary is missing beside {resolution.path}")
    git_idx = git_index_binary if git_index_binary.is_file() and os.access(git_index_binary, os.X_OK) else None
    return search_binary, index_binary, git_idx


def _run_command(
    command: list[str],
    *,
    check: bool = True,
    timeout: int | float = 60,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip() or completed.stdout.strip() or f"command failed: {' '.join(command)}"
        )
    return completed


def _mirror_entry(source: Path, target: Path) -> None:
    # Never follow symlinks.  ``Path.is_dir()`` resolves them, so a
    # self-referential link such as ``/usr/bin/X11 -> .`` (standard on
    # Debian/Ubuntu) would recurse forever, physically copying the whole
    # subtree at every level.  A code mirror has no business duplicating link
    # targets anyway (cycles, out-of-tree escapes), so drop them outright.
    if source.is_symlink() or not source.exists():
        return
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            for child in sorted(source.iterdir()):
                if child.name in _SKIP_ROOTS or child.name.startswith("."):
                    continue
                _mirror_entry(child, target / child.name)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = source.stat().st_mode
    except OSError:
        return
    try:
        if mode & 0o004:
            os.link(source, target)
            return
        raise OSError
    except OSError:
        shutil.copy2(source, target)
        with suppress(OSError):
            target.chmod(mode | 0o444)


def _start_bridge(container_id: str) -> subprocess.Popen[str]:
    script = (
        "set -eu\n"
        "while IFS= read -r encoded; do\n"
        "  printf '%s' \"$encoded\" | base64 -d > /tmp/lemoncrow-zoekt-query.json\n"
        "  wget -qO- --header='Content-Type: application/json' "
        "--post-file=/tmp/lemoncrow-zoekt-query.json http://127.0.0.1:6070/api/search\n"
        f"  printf '\\n{_BRIDGE_SENTINEL}\\n'\n"
        "done\n"
    )
    return subprocess.Popen(
        ["docker", "exec", "-i", container_id, "sh", "-lc", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


_SERVERS: dict[str, ZoektServer] = {}
_SERVERS_LOCK = threading.Lock()


def get_zoekt_server(repo_root: str | Path, *, resolution: ZoektBinaryResolution | None = None) -> ZoektServer:
    root = Path(repo_root).resolve()
    key = str(root)
    with _SERVERS_LOCK:
        server = _SERVERS.get(key)
        if server is None:
            server = ZoektServer(root, resolution=resolution)
            _SERVERS[key] = server
        elif resolution is not None and server.resolution is None:
            server.resolution = resolution
    return server


def reset_zoekt_servers() -> None:
    with _SERVERS_LOCK:
        servers = list(_SERVERS.values())
        _SERVERS.clear()
    for server in servers:
        server.stop()


atexit.register(reset_zoekt_servers)


__all__ = ["ZoektHealth", "ZoektServer", "get_zoekt_server", "reset_zoekt_servers"]
