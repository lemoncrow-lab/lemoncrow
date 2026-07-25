"""Ledger + session-resolution substrate (per-request run ledger, live session id).

Leaf module holding the process-global ledger/session state and its accessors,
imported by the dispatch loop (mcp_server) and by the engine tools. State lives
here; mcp_server reaches the few sites it still writes via ``ledger._name``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict, deque
from typing import Any

from lemoncrow.gateway.adapters.mcp.session_state import _lemoncrow_root, _mcp_session_file
from lemoncrow.gateway.adapters.mcp.smart_state import _STATE_LOCK
from lemoncrow.infra.runtime.realtime_context import RealtimeContextManager
from lemoncrow.infra.runtime.run_ledger import RunLedger

_log = logging.getLogger("lemoncrow.mcp")

_current_ledger: RunLedger | None = None

_request_ledger: threading.local = threading.local()

# Request-scoped session identity (singleton daemon path). The bridge -- a child
# of the claude window -- resolves the live session id + host label and passes
# them as headers; the daemon (not a child of the window) cannot self-resolve
# them, so _dispatch stamps this thread-local for the duration of one request and
# the resolvers below prefer it. Unset on the stdio path -> legacy behaviour.
_request_session: threading.local = threading.local()

_http_session_ledgers: OrderedDict[str, RunLedger] = OrderedDict()

_http_session_ledgers_lock = threading.Lock()

_MAX_HTTP_SESSION_LEDGERS = 64

_realtime_ctx: RealtimeContextManager | None = None

_product_session_id: str | None = None

_cached_claude_session_id: str = ""

_cached_mcp_model: str = ""

_WINDOW_SID_CACHE: tuple[float, str] | None = None

_MCP_WINDOW_ID: tuple[int, int] | None = None

_MCP_WINDOW_ID_RESOLVED = False


def _ledger_for_session(session_id: str) -> RunLedger:
    """Per-session ledger so concurrent HTTP clients don't co-mingle into the
    process-global ledger. Bounded LRU; the least-recently-used entry is evicted
    past the cap. On a cache miss an existing ``run.json`` is rehydrated so an
    evicted-then-reused session does not overwrite its own accumulated events.

    Uses the global store root (not the workspace-scoped one): a session is
    already globally unique by id and lives under the canonical
    ``sessions/YYYY/MM/DD/<host>/<id>/`` tree regardless of which workspace
    happens to be resolved for this request.
    """
    from lemoncrow.core.foundation.paths import default_store_root, find_session_dir

    root = default_store_root()
    with _http_session_ledgers_lock:
        led = _http_session_ledgers.get(session_id)
        if led is not None:
            _http_session_ledgers.move_to_end(session_id)
            return led
        existing_dir = find_session_dir(root, session_id)
        if existing_dir is not None:
            path = existing_dir / "run.json"
            if path.exists():
                try:
                    led = RunLedger.load(path)
                    # load() builds the ledger without a root; restore it so the
                    # rehydrated ledger persists back to the same run.json.
                    led._root = root
                except ValueError:
                    led = None
        if led is None:
            led = RunLedger(root=root, agent=_detect_agent(), session_id=session_id)
        if len(_http_session_ledgers) >= _MAX_HTTP_SESSION_LEDGERS:
            _http_session_ledgers.popitem(last=False)
        _http_session_ledgers[session_id] = led
        return led


def _set_request_session(session_id: str | None, host: str = "", model: str = "", bridge: str = "") -> Any:
    """Stamp the per-request session identity on the CURRENT thread; return prior.

    Set by the HTTP dispatcher from the client's headers so every session-id /
    host consumer inside ``_handle`` (debug log path, savings sidecar, telemetry,
    session-scoped run dirs) resolves the *calling* session rather than the
    daemon process's own (wrong) window/env. ``bridge`` tags any managed bash a
    tool starts so it can be reaped when that bridge disconnects. All-empty input
    clears the context.
    """
    prior = getattr(_request_session, "value", None)
    sid = (session_id or "").strip()
    h = (host or "").strip()
    m = (model or "").strip()
    b = (bridge or "").strip()
    _request_session.value = {"session_id": sid, "host": h, "model": m, "bridge": b} if (sid or h or m or b) else None
    return prior


def _clear_request_session(prior: Any) -> None:
    _request_session.value = prior


def _request_bridge_id() -> str:
    """Bridge id of the in-flight request (owner tag for managed bash), or ""."""
    ctx = getattr(_request_session, "value", None)
    return str(ctx["bridge"]) if ctx and ctx.get("bridge") else ""


def _request_session_identity() -> tuple[str, str]:
    """``(session_id, host)`` stamped on this thread by the daemon dispatcher.

    ``("", "")`` on the stdio path (no header context). Every session-scoped
    writer must consult this FIRST: the singleton daemon is not a child of any
    ``claude`` window, so its own window/env resolution names whichever session
    happened to start it and would credit every other window's work there.
    """
    ctx = getattr(_request_session, "value", None)
    if not ctx:
        return "", ""
    return str(ctx.get("session_id") or "").strip(), str(ctx.get("host") or "").strip()


def _set_request_ledger(session_id: str | None) -> Any:
    """Scope _get_ledger() to a per-session ledger on the CURRENT thread; returns
    the prior value to restore. A falsy session_id is a no-op (stdio / no session
    header keeps the process-global ledger)."""
    prior = getattr(_request_ledger, "value", None)
    if session_id:
        _request_ledger.value = _ledger_for_session(session_id)
    return prior


def _clear_request_ledger(prior: Any) -> None:
    _request_ledger.value = prior


def _get_ledger() -> RunLedger:
    req = getattr(_request_ledger, "value", None)
    if isinstance(req, RunLedger):
        return req
    global _current_ledger
    if _current_ledger is not None:
        return _current_ledger
    # Bind the ledger to the host session id (Claude Code UUID, etc.) so
    # run.json lands at sessions/YYYY/MM/DD/<host>/<host-id>/run.json — the
    # same canonical folder the plugin hooks read and the savings sidecar
    # writes. The global store root (not workspace-scoped): a session is
    # already globally unique by id, regardless of which workspace happens to
    # be resolved for this process. Computed outside the lock since
    # _get_claude_session_id touches shared state; non-host runs fall back to
    # RunLedger's own random uuid4.
    host_sid = _get_claude_session_id() or None
    with _STATE_LOCK:
        if _current_ledger is None:
            from lemoncrow.core.foundation.paths import default_store_root

            _current_ledger = RunLedger(root=default_store_root(), agent=_detect_agent(), session_id=host_sid)
    return _current_ledger


def _get_realtime_context() -> RealtimeContextManager:
    global _realtime_ctx
    with _STATE_LOCK:
        if _realtime_ctx is None:
            _realtime_ctx = RealtimeContextManager(_lemoncrow_root())
    return _realtime_ctx


def _get_product_session_id() -> str:
    global _product_session_id
    with _STATE_LOCK:
        if _product_session_id is None:
            from lemoncrow.core.foundation.identity import new_session_id

            _product_session_id = new_session_id()
    return _product_session_id


def _detect_agent() -> str:
    """Derive the agent/host label from the runtime environment.

    Thin re-export: the env-var sniffing lives once, canonically, in
    ``lemoncrow.core.foundation.paths.detect_host`` so every hook script across
    every integration (not just this MCP server) resolves the identical host
    label -- the same value that segregates each host's session storage.
    """
    ctx = getattr(_request_session, "value", None)
    if ctx and ctx.get("host"):
        return str(ctx["host"])

    from lemoncrow.core.foundation.paths import detect_host

    return detect_host()


def _mcp_window_id() -> tuple[int, int] | None:
    global _MCP_WINDOW_ID, _MCP_WINDOW_ID_RESOLVED
    if not _MCP_WINDOW_ID_RESOLVED:
        from lemoncrow.core.foundation.session_window import host_window_id

        _MCP_WINDOW_ID = host_window_id()
        _MCP_WINDOW_ID_RESOLVED = True
    return _MCP_WINDOW_ID


def _resolve_live_session_id() -> str:
    """Live session id for this MCP server's window.

    Anchors to the ``claude`` window process (stable across ``/clear``, unique
    per window) via its own per-window identity file, falling back to the launch
    env var. A long-lived server tracks the live session across ``/clear`` and
    never adopts a sibling session's id from a shared workspace slot.
    """
    global _WINDOW_SID_CACHE
    from lemoncrow.core.foundation.session_window import resolve_window_session_id, window_file_path

    root = _lemoncrow_root()
    ws_hash = _workspace_ws_hash()
    win = _mcp_window_id()
    mtime = 0.0
    if win is not None:
        try:
            mtime = window_file_path(root, ws_hash, win[0], win[1]).stat().st_mtime
        except OSError:
            mtime = 0.0
    cached = _WINDOW_SID_CACHE
    if cached is not None and cached[0] == mtime:
        return cached[1]
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    sid = resolve_window_session_id(root, ws_hash, env_session_id=env_sid)
    _WINDOW_SID_CACHE = (mtime, sid)
    return sid


def _workspace_ws_hash() -> str:
    from lemoncrow.core.foundation.session_window import workspace_hash

    ws = os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd()
    return workspace_hash(ws)


def _get_claude_session_id() -> str:
    """Return the Claude Code session UUID.

    Resolves the window-anchored live id first (``_resolve_live_session_id`` ->
    this window's own identity file), caching it in _cached_claude_session_id.
    Falls back to the cached value, then the MCP registration file, then the
    product session UUID.
    """
    global _cached_claude_session_id, _cached_mcp_model

    # Singleton daemon: the request carries the caller's session id in a header
    # (the daemon can't resolve its own window). Prefer it, and never cache it
    # into the process-global slot -- that would leak one session's id to the
    # next request on this pooled worker thread.
    ctx = getattr(_request_session, "value", None)
    if ctx and ctx.get("session_id"):
        return str(ctx["session_id"])

    # Window-anchored live id: correct across /clear and immune to sibling
    # sessions sharing the workspace bridge (resolver is mtime-cached).
    sid = _resolve_live_session_id()
    if sid:
        _cached_claude_session_id = sid
        return sid
    if _cached_claude_session_id:
        return _cached_claude_session_id

    try:
        f = _mcp_session_file()
        if f.is_file():
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                sid = str(data.get("claude_session_id") or "").strip()
                if sid:
                    _cached_claude_session_id = sid
                    _cached_mcp_model = str(data.get("model") or "").strip()
                    return sid
    except (OSError, json.JSONDecodeError):
        _log.debug("MCP session id read failed", exc_info=True)
    return _get_product_session_id()


# Per-session paths served IN FULL via `read` (mode == "full"). Same session-
# bucketing/time-window rationale as mcp_server's _RECENT_CODE_SEARCH_QUERIES
# (HTTP: per client session; stdio: one process-wide "_global" bucket per host
# window). Feeds _check_redundant_file_dump: a bash `cat`/`head`/`tail`/
# `sed -n ...p` of a path the model already has the complete content of (from
# THIS session's `read` call) adds nothing -- confirmed in a 2026-07-25
# debt-benchmark rep: `read debt.py:full`, then `sed -n '60,160p' debt.py` via
# bash, then `cat debt.py` via bash -- three servings of the same ~130-line
# file, each re-sent on every later turn's resent history. Lives here (not
# mcp_server) so bash.py can consume it without a back-dependency on
# mcp_server -- mcp_server records, bash.py checks, both import this leaf
# module. Time-bounded like the query bucket so a stale entry from a much
# earlier, unrelated conversation on the same long-lived daemon never blocks a
# legitimate re-read.
_RECENT_FULL_READS: OrderedDict[str, deque[tuple[float, str]]] = OrderedDict()

_recent_full_reads_lock = threading.Lock()

_MAX_RECENT_FULL_READ_SESSIONS = _MAX_HTTP_SESSION_LEDGERS

_RECENT_FULL_READ_HISTORY = 12

_RECENT_FULL_READ_WINDOW_SECONDS = 180.0

_BASH_FILE_DUMP_RE = re.compile(
    r"^\s*cat(?:\s+-\S+)*\s+(?P<path1>\S+)\s*$"
    r"|^\s*(?:head|tail)(?:\s+-\S+(?:\s+\d+)?)*\s+(?P<path2>\S+)\s*$"
    r"|^\s*sed\s+-n\s+\S+\s+(?P<path3>\S+)\s*$"
)


def _recent_full_reads() -> deque[tuple[float, str]]:
    """The current session's recent-full-read bucket (see _RECENT_FULL_READS)."""
    led = getattr(_request_ledger, "value", None)
    sid = led.session_id if isinstance(led, RunLedger) and led.session_id else "_global"
    with _recent_full_reads_lock:
        bucket = _RECENT_FULL_READS.get(sid)
        if bucket is None:
            if len(_RECENT_FULL_READS) >= _MAX_RECENT_FULL_READ_SESSIONS:
                _RECENT_FULL_READS.popitem(last=False)
            bucket = deque(maxlen=_RECENT_FULL_READ_HISTORY)
            _RECENT_FULL_READS[sid] = bucket
        else:
            _RECENT_FULL_READS.move_to_end(sid)
        return bucket


def _record_full_read(path: Any) -> None:
    """Record that *path* was just served IN FULL via `read`, for
    _check_redundant_file_dump below. Best-effort: never raises."""
    from pathlib import Path

    try:
        resolved = str(Path(path).resolve())
    except OSError:
        return
    _recent_full_reads().append((time.monotonic(), resolved))


def _check_redundant_file_dump(command: str, cwd: str | None) -> str | None:
    """Terse redirect when *command* is a bare cat/head/tail/sed-print of a path
    already served in full via `read` this session, within
    _RECENT_FULL_READ_WINDOW_SECONDS.

    A hard backstop for the tool-discipline guidance ("bash = execution only,
    never cat/sed a file already read") -- textual guidance alone did not stop
    it under verification pressure (measured in the same debt-benchmark rep
    that motivated _RECENT_CODE_SEARCH_QUERIES). Returns None when *command*
    doesn't match a bare dump, or the path wasn't recently read in full --
    never blocks a compound command (pipes/redirects/`&&`) or a fresh path.
    """
    from pathlib import Path

    m = _BASH_FILE_DUMP_RE.match(command)
    if not m:
        return None
    raw_path = next((g for g in m.groups() if g), None)
    if not raw_path:
        return None
    base = Path(cwd) if cwd else Path(os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd())
    try:
        target = Path(raw_path)
        resolved = str(target.resolve()) if target.is_absolute() else str((base / target).resolve())
    except OSError:
        return None
    now = time.monotonic()
    for ts, p in _recent_full_reads():
        if p == resolved and now - ts <= _RECENT_FULL_READ_WINDOW_SECONDS:
            return "[lc: already read in full this session -- reuse that content, don't re-dump]"
    return None
