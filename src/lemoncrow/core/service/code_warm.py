"""Daemon-owned code-index warmer.

Keeps the shared code-context SQLite index warm for every active MCP
workspace so the per-request MCP path never has to trigger a synchronous
cold build.  The service daemon owns this; it scans the ``mcp_sessions``
registry, fires ``lc code index`` as a detached subprocess per active
workspace, and lets the subprocess write the SQLite index and exit —
releasing all indexing memory (ProcessPool, AST data, CoW pages) after
each run.  The parent daemon process footprint is unaffected.

Gated by ``LEMONCROW_SERVICE_CODE_WARM`` (default on).  Set to one of
``0``/``false``/``no``/``off`` to disable.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from lemoncrow.core.foundation.paths import default_store_root, workspace_key

logger = logging.getLogger(__name__)

_POLL_SECONDS = 15.0
_DISABLED_VALUES = {"0", "false", "no", "off"}


def _skip_ephemeral_rewarm(workspace: Path) -> bool:
    """True when *workspace* is ephemeral (under a system temp root) AND
    already has an index — temp workspaces are indexed once, never re-warmed.

    Bench/provisioning workspaces live under ``/tmp``; a daemon restart
    resets the fired-set and would otherwise re-index them from scratch
    (hours for a linux-sized clone) for no interactive benefit.
    """
    tmp_roots = {Path(tempfile.gettempdir()).resolve(), Path("/var/tmp"), Path("/dev/shm")}
    if not any(root == workspace or root in workspace.parents for root in tmp_roots):
        return False
    db = default_store_root() / "workspaces" / workspace_key(workspace) / "code_context.sqlite"
    return db.exists()


def _warm_enabled() -> bool:
    raw = os.getenv("LEMONCROW_SERVICE_CODE_WARM", "1").strip().lower()
    return raw not in _DISABLED_VALUES


def _mcp_sessions_dir() -> Path:
    return default_store_root() / "mcp_sessions"


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _registered_mcp_pid_is_live(pid: int) -> bool:
    if not _pid_is_running(pid):
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    if not cmdline.exists():
        return True
    try:
        parts = [part for part in cmdline.read_bytes().split(b"\0") if part]
    except OSError:
        return False
    text = " ".join(part.decode("utf-8", errors="ignore") for part in parts)
    return ("lc" in text or "lc" in text) and "mcp" in text


def discover_workspaces() -> list[Path]:
    """Return resolved workspace dirs from the mcp_sessions registry.

    Only existing directories from live MCP processes are returned.  Discovery
    is limited to the registry -- the service cwd is intentionally never
    auto-added so that a daemon with no active MCP sessions warms nothing.
    """
    sessions_dir = _mcp_sessions_dir()
    if not sessions_dir.is_dir():
        return []
    seen: set[Path] = set()
    workspaces: list[Path] = []
    for entry in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.debug("skipping unreadable mcp session file: %s", entry, exc_info=True)
            continue
        pid = data.get("pid") if isinstance(data, dict) else None
        if not isinstance(pid, int) or not _registered_mcp_pid_is_live(pid):
            try:
                entry.unlink()
            except OSError:
                logger.debug("failed to prune dead mcp session file: %s", entry, exc_info=True)
            continue
        ws = data.get("workspace") if isinstance(data, dict) else None
        if not isinstance(ws, str) or not ws.strip():
            continue
        try:
            resolved = Path(ws).expanduser().resolve()
        except OSError:
            continue
        if not resolved.is_dir():
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        workspaces.append(resolved)
    return workspaces


def _fire_index_subprocess(workspace: Path) -> None:
    """Launch ``lc code index`` in a fully detached child process.

    Fire-and-forget: the caller does not wait for completion.  The subprocess
    acquires the SQLite write-lock, indexes the workspace, and exits — all
    memory used for indexing (ProcessPool, AST data, etc.) is released when
    it exits, leaving the parent process footprint unchanged.
    """
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "lemoncrow.gateway.cli",
            "code",
            "index",
            "--repo-root",
            str(workspace),
            "--no-stats",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


class _CodeWarmer:
    """Background loop that fires index subprocesses per active workspace.

    Replaces the old engine-retention approach: instead of creating and
    holding a ``CodeContextEngine`` per workspace (which dragged the full
    ProcessPool heap into the daemon), we just fire a detached subprocess.
    The subprocess does all the heavy work and exits cleanly.
    """

    def __init__(self, *, poll_seconds: float = _POLL_SECONDS) -> None:
        self._poll_seconds = poll_seconds
        # Workspaces where an index subprocess was already fired; prevents
        # re-firing on every poll cycle (only new workspaces get a subprocess).
        self._fired: set[Path] = set()
        self._stop = threading.Event()
        # NB: not named ``_thread`` -- mypyc emits that as a C struct field
        # ``__thread``, which clang rejects as the reserved TLS keyword.
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._loop,
            name="lemoncrow-code-warmer",
            daemon=True,
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    def _warm_once(self) -> None:
        from lemoncrow.core.capabilities import licensing

        workspaces = discover_workspaces()
        if not licensing.has_feature("unlimited_repos"):
            # unlimited_repos is a free feature (see licensing/features.py), so
            # this cap is unreachable today; kept as the seam if that changes.
            workspaces = workspaces[:1]
        for workspace in workspaces:
            if workspace in self._fired:
                continue
            if _skip_ephemeral_rewarm(workspace):
                self._fired.add(workspace)
                logger.info("code warmer: skipping ephemeral already-indexed workspace %s", workspace)
                continue
            try:
                _fire_index_subprocess(workspace)
                self._fired.add(workspace)
                logger.info("code warmer: fired index subprocess for %s", workspace)
            except Exception:
                logger.exception("code warmer: failed to fire subprocess for %s", workspace)

    def _loop(self) -> None:
        # First pass immediately, then poll for newly registered sessions.
        try:
            self._warm_once()
        except Exception:
            logger.exception("code warmer: warm pass failed")
        while not self._stop.wait(self._poll_seconds):
            try:
                self._warm_once()
            except Exception:
                logger.exception("code warmer: warm pass failed")


# --- stdio MCP single-workspace warmer (Workstream 6 / G10) ----------------
#
# The SERVICE path warms every active workspace via ``_CodeWarmer`` above.  The
# stdio MCP server (``mcp_server.serve``) instead owns exactly one workspace
# and is not warmed by the service daemon, so it pays cold-start on
# Zoekt/ast-grep subprocesses at the first code-context tool call.
# ``warm_stdio_workspace`` fires a detached ``lc code index`` subprocess
# for that workspace on startup.  It is idempotent and fail-open.

_stdio_warmed: Path | None = None
_stdio_lock = threading.Lock()


def warm_stdio_workspace(workspace: str | Path) -> bool:
    """Warm the code-context index for a single stdio workspace (idempotent).

    Fires ``lc code index`` as a detached subprocess so the parent MCP
    process never takes on the indexing memory cost.  Returns ``True`` when a
    subprocess was launched, ``False`` when warming was skipped (disabled,
    missing dir, already warm) or failed.  Never raises.
    """
    global _stdio_warmed
    if not _warm_enabled():
        logger.info("stdio code warmer disabled via LEMONCROW_SERVICE_CODE_WARM")
        return False
    try:
        resolved = Path(workspace).expanduser().resolve()
    except OSError:
        logger.exception("stdio code warmer: cannot resolve workspace %s", workspace)
        return False
    if not resolved.is_dir():
        logger.debug("stdio code warmer: workspace is not a directory: %s", resolved)
        return False
    with _stdio_lock:
        if _stdio_warmed == resolved:
            return False
        if _skip_ephemeral_rewarm(resolved):
            _stdio_warmed = resolved
            logger.info("stdio code warmer: skipping ephemeral already-indexed workspace %s", resolved)
            return False
        try:
            _fire_index_subprocess(resolved)
            _stdio_warmed = resolved
            logger.info("stdio code warmer: fired index subprocess for %s", resolved)
            return True
        except Exception:
            logger.exception("stdio code warmer: failed to fire subprocess for %s", resolved)
            return False


_warmer: _CodeWarmer | None = None
_warmer_lock = threading.Lock()


def start_code_warmer() -> _CodeWarmer | None:
    """Start the daemon code-index warmer (idempotent).

    Returns ``None`` when disabled via ``LEMONCROW_SERVICE_CODE_WARM``; otherwise
    returns the singleton warmer (already started).
    """
    global _warmer
    if not _warm_enabled():
        logger.info("code warmer disabled via LEMONCROW_SERVICE_CODE_WARM")
        return None
    with _warmer_lock:
        if _warmer is None:
            _warmer = _CodeWarmer()
            _warmer.start()
            logger.info("code warmer started")
        return _warmer
