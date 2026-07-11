"""Window-anchored session-id resolution.

A long-lived MCP server (one per Claude window) must learn the *live* session
id even after ``/clear`` or ``--resume`` mints a new id under it, and must not
be confused by sibling sessions that share the same workspace directory.

Neither of the two obvious signals is sufficient on its own:

* ``CLAUDE_CODE_SESSION_ID`` is set per MCP process at launch, so it is correct
  for concurrent windows -- but it is *frozen at launch* and goes stale the
  moment the user runs ``/clear`` (the MCP server outlives the session id).
* ``workspaces/<hash>/session_state.json`` is rewritten by SessionStart on every
  ``/clear`` so it always names the live session -- but it is a single shared
  slot, so concurrent windows in one workspace clobber each other's value.

This module anchors resolution to the **window process**: the ``claude``
process that is the common ancestor of both the MCP server and the hook
processes. That pid is stable across ``/clear`` (the window is the same) and
unique per window (siblings have different ``claude`` pids). SessionStart
writes a per-window file named ``<window_pid>-<window_btime>.json``; the MCP
server reads its own window's file. One writer per file, so concurrent windows
in a shared workspace never clobber each other. Both sides run on LemonCrow's venv python
(hooks via ``_run_hook.sh``), so this one module serves both.

Linux resolves the ancestry via ``/proc``; macOS/BSD via one ``ps`` table
sweep. When no ``claude`` ancestor is found :func:`host_window_id` returns
``None`` and callers fall back to the env var, preserving today's behavior.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path

_log = logging.getLogger(__name__)

# Process names that own a long-lived MCP server + the session lifecycle. Only
# Claude exhibits the launch-env-goes-stale-on-/clear problem today; other hosts
# set a per-session env var the callers read directly.
_HOST_PROCESS_NAMES = frozenset({"claude"})


def _read_proc_stat(pid: int) -> tuple[int, int, str] | None:
    """Return ``(ppid, starttime_ticks, comm)`` for *pid*, or ``None``.

    Parses ``/proc/<pid>/stat``. ``comm`` (field 2) can contain spaces and
    parentheses, so split on the last ``)`` before reading the numeric fields.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    rparen = data.rfind(b")")
    lparen = data.find(b"(")
    if rparen < 0 or lparen < 0 or rparen < lparen:
        return None
    comm = data[lparen + 1 : rparen].decode("utf-8", "replace")
    fields = data[rparen + 2 :].split()
    # After '(comm)' the fields are 1-indexed in proc(5); fields[0] is 'state'
    # (field 3), so ppid (field 4) is fields[1] and starttime (field 22) is
    # fields[19].
    try:
        ppid = int(fields[1])
        starttime = int(fields[19])
    except (IndexError, ValueError):
        return None
    return ppid, starttime, comm


def _argv0_basename(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            first = fh.read().split(b"\x00", 1)[0]
    except OSError:
        return ""
    return os.path.basename(first.decode("utf-8", "replace"))


# `ps -o lstart=` format under LC_ALL=C: "Fri Jul 10 18:17:26 2026".
_PS_LSTART_FMT = "%a %b %d %H:%M:%S %Y"


def _ps_proc_table() -> dict[int, tuple[int, int, str]]:
    """``pid -> (ppid, starttime_epoch, name)`` via ONE ``ps`` sweep (macOS/BSD).

    The whole table in a single subprocess so the ancestor walk needs no
    per-hop ``ps`` calls. ``lstart`` (second resolution) stands in for Linux's
    starttime ticks as the PID-reuse guard -- writers and readers on the same
    platform derive the same value, which is all the key needs. Empty dict on
    any failure (callers then return ``None`` -> env fallback).
    """
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,lstart=,comm="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env={**os.environ, "LC_ALL": "C"},
        )
    except Exception:  # noqa: BLE001 -- optional session probe must fail open.
        return {}
    table: dict[int, tuple[int, int, str]] = {}
    for line in proc.stdout.splitlines():
        # pid ppid dow mon day time year comm... (comm may contain spaces)
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            btime = int(time.mktime(time.strptime(" ".join(parts[2:7]), _PS_LSTART_FMT)))
        except (ValueError, OverflowError):
            continue
        table[pid] = (ppid, btime, os.path.basename(parts[7]))
    return table


def _ps_host_window_id(start_pid: int) -> tuple[int, int] | None:
    """``host_window_id`` for /proc-less platforms (macOS/BSD) via ``ps``."""
    table = _ps_proc_table()
    pid = start_pid
    seen: set[int] = set()
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        entry = table.get(pid)
        if entry is None:
            return None
        ppid, btime, name = entry
        if name in _HOST_PROCESS_NAMES:
            return pid, btime
        pid = ppid
    return None


def host_window_id(start_pid: int | None = None) -> tuple[int, int] | None:
    """Return ``(pid, starttime)`` of the nearest ``claude`` ancestor, or ``None``.

    Walks the process-parent chain from *start_pid* (default: this process) --
    via ``/proc`` on Linux, via one ``ps`` table sweep on macOS/BSD.
    ``starttime`` (proc start ticks on Linux, epoch seconds elsewhere) is
    included so callers can guard against PID reuse: a recycled pid will have
    a different start time. Returns ``None`` on any read error or when no
    ``claude`` ancestor exists -- callers then fall back to env-based
    resolution.
    """
    pid = start_pid if start_pid is not None else os.getpid()
    if not os.path.isdir("/proc"):
        return _ps_host_window_id(pid)
    seen: set[int] = set()
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        st = _read_proc_stat(pid)
        if st is None:
            return None
        ppid, starttime, comm = st
        if comm in _HOST_PROCESS_NAMES or _argv0_basename(pid) in _HOST_PROCESS_NAMES:
            return pid, starttime
        pid = ppid
    return None


def workspace_hash(workspace: str | os.PathLike[str]) -> str:
    """Human-readable workspace key, matching the SessionStart hook + MCP server."""
    from lemoncrow.core.foundation.paths import workspace_key

    return workspace_key(Path(workspace).resolve())


def windows_dir(root: str | os.PathLike[str], ws_hash: str) -> Path:
    """Directory holding this workspace's per-window identity files."""
    return Path(root) / "workspaces" / ws_hash / "windows"


def window_file_path(root: str | os.PathLike[str], ws_hash: str, pid: int, btime: int) -> Path:
    """Path to one window's identity file, keyed by ``(pid, btime)``."""
    return windows_dir(root, ws_hash) / f"{int(pid)}-{int(btime)}.json"


def _read_window_session_id(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("session_id") or "").strip()


def _pid_alive(pid: int) -> bool:
    """True if *pid* is a live process. Best-effort; assumes alive on error."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _prune_dead_windows(directory: Path, *, keep: str) -> None:
    """Best-effort removal of window files whose pid is no longer alive.

    Bounds accumulation across days of sessions in one workspace (replaces the
    old append-only-registry row cap). Never removes ``keep`` (this window's own
    file). Fail-open.
    """
    try:
        entries = list(directory.glob("*.json"))
    except OSError:
        return
    for entry in entries:
        if entry.name == keep:
            continue
        pid_str = entry.stem.partition("-")[0]
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if not _pid_alive(pid):
            with contextlib.suppress(OSError):
                entry.unlink()


def register_window_session(
    root: str | os.PathLike[str],
    ws_hash: str,
    *,
    session_id: str,
    source: str = "",
    transcript_path: str = "",
    model: str = "",
) -> None:
    """Write *this* window's identity file. Called by the SessionStart hook.

    Records the live ``session_id`` under this window's ``(window_pid,
    window_btime)`` so the MCP server -- whose launch env id may be stale --
    recovers the live id by reading its own window's file. Each window writes
    only its own file, so concurrent windows sharing a workspace never clobber
    each other (no shared slot, no read-modify-write, no lock). Best-effort:
    failures never raise (the hook is fail-open). No-op when no ``claude``
    window ancestor is found (non-Linux / hostless) -- callers fall back to the
    env var.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return
    win = host_window_id()
    if win is None:
        return
    pid, btime = win
    payload = {
        "session_id": session_id,
        "source": source,
        "transcript_path": transcript_path,
        "model": model,
        "ts": time.time(),
        "window_pid": pid,
        "window_btime": btime,
    }
    directory = windows_dir(root, ws_hash)
    path = window_file_path(root, ws_hash, pid, btime)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Unique temp name per writer so even two registers for the SAME window
        # can't corrupt each other -- last atomic replace wins, no partial file.
        tmp = directory / f"{path.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        _log.debug("window-session file write failed", exc_info=True)
        return
    _prune_dead_windows(directory, keep=path.name)


def resolve_window_session_id(
    root: str | os.PathLike[str],
    ws_hash: str,
    *,
    env_session_id: str = "",
) -> str:
    """Resolve the live session id for *this* process's window.

    Priority:
      1. This window's own identity file (keyed by ``(window_pid,
         window_btime)``) -- written by SessionStart, correct across ``/clear``
         and immune to sibling windows sharing the workspace.
      2. ``env_session_id`` (the launch env var) -- correct before SessionStart
         has written the file, and the only signal on non-Linux hosts.
      3. ``""`` when nothing is known.
    """
    win = host_window_id()
    if win is not None:
        sid = _read_window_session_id(window_file_path(root, ws_hash, win[0], win[1]))
        if sid:
            return sid
    env = (env_session_id or "").strip()
    if env:
        return env
    return ""
