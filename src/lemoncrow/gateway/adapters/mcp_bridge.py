"""Thin stdio<->HTTP bridge to the per-workspace singleton MCP daemon.

Hosts spawn ``lc mcp`` expecting a newline-delimited stdio JSON-RPC server. In
singleton mode this bridge takes that role: it resolves the workspace, ensures
the shared daemon is running (:func:`mcp_daemon.ensure_daemon`), and forwards
each JSON-RPC request to the daemon's loopback ``/mcp`` endpoint, streaming
responses back to stdout.

It holds no thread pools, code index, embedder, or runtime -- all the heavy
state lives once in the daemon -- so N concurrent host sessions cost N near-empty
proxies instead of N full stdio servers. Requests are forwarded concurrently
(the host pipelines calls; JSON-RPC responses are matched by ``id``, not order),
with a single serialized stdout writer.

Per-session identity travels in headers, not the daemon's process env: the
bridge is a child of the ``claude`` window, so it -- not the shared daemon --
can resolve the live session id (correct across ``/clear``) and the host label,
and passes them as ``Mcp-Session-Id`` / ``X-LemonCrow-Agent``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.paths import default_store_root, detect_host
from lemoncrow.core.foundation.session_window import resolve_window_session_id, workspace_hash
from lemoncrow.gateway.adapters.mcp_daemon import ensure_daemon

_log = logging.getLogger("lemoncrow.mcp")

_HOST_WORKSPACE_VARS = ("CLAUDE_WORKSPACE_ROOT", "LEMONCROW_WORKSPACE_ROOT", "VSCODE_CWD")
_MAX_INFLIGHT = 32
# Ping cadence that keeps this bridge's session "attached" on the daemon. Must
# stay well under the daemon's _SESSION_TTL_SECONDS so a single dropped ping
# doesn't detach a live session.
_PING_INTERVAL_SECONDS = 60.0


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _resolve_workspace() -> str:
    """Resolve the workspace this bridge serves (mirrors ``mcp_server.main``).

    Host-injected env var wins; else the enclosing git repo; else cwd.
    """
    for var in _HOST_WORKSPACE_VARS:
        val = os.environ.get(var)
        if val:
            return str(Path(val).expanduser().resolve())
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return str(Path(result.stdout.strip()).resolve())
    except (OSError, subprocess.SubprocessError):
        _log.debug("git workspace detection failed", exc_info=True)
    return str(Path.cwd().resolve())


class _DaemonHandle:
    """Thread-safe holder for the current daemon registration + respawn.

    A daemon that idle-reaped (or crashed) mid-session is transparently
    respawned; concurrent forwarders that all hit the dead daemon serialize on
    the lock so only one respawn happens per generation.
    """

    def __init__(self, workspace: str, root: Path) -> None:
        self._workspace = workspace
        self._root = root
        self._lock = threading.Lock()
        self._reg = ensure_daemon(workspace, root)

    def current(self) -> dict[str, Any]:
        with self._lock:
            return self._reg

    def respawn(self, stale: dict[str, Any]) -> dict[str, Any]:
        """Ensure a live daemon, but only re-ensure once per stale generation."""
        with self._lock:
            # Another forwarder already refreshed past the generation this caller
            # saw as dead -> reuse its result instead of ensuring again.
            if self._reg is not stale:
                return self._reg
            self._reg = ensure_daemon(self._workspace, self._root)
            return self._reg


def run_bridge(root: str | os.PathLike[str] | None = None) -> None:
    """Run the stdio bridge until the host closes stdin (blocks)."""
    import httpx

    resolved_root = default_store_root() if root is None else Path(root)
    workspace = _resolve_workspace()
    ws_hash = workspace_hash(workspace)
    host = detect_host()
    # Stable per-bridge liveness id (distinct from the Mcp-Session-Id, which is
    # for ledger/attribution and can change across /clear). Anchors this bridge
    # in the daemon's attached-session registry so an idle-but-open session
    # keeps the shared daemon alive.
    bridge_id = uuid.uuid4().hex
    handle = _DaemonHandle(workspace, resolved_root)

    # No overall timeout: tool calls (bash, web_fetch) can run long, exactly as
    # they did on the stdio server. A short connect timeout still fails fast when
    # the daemon is gone, triggering a single respawn+retry.
    client = httpx.Client(timeout=httpx.Timeout(None, connect=10.0))
    stdout_lock = threading.Lock()

    def _write(message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False) + "\n"
        with stdout_lock:
            sys.stdout.write(payload)
            sys.stdout.flush()

    def _post(req: dict[str, Any]) -> dict[str, Any] | None:
        request_id = req.get("id")
        # Resolve the live session id per request: it changes across /clear, and
        # only the bridge (child of the claude window) can resolve it correctly.
        sid = resolve_window_session_id(
            resolved_root, ws_hash, env_session_id=os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        )
        body = json.dumps(req)
        for attempt in (0, 1):
            reg = handle.current()
            headers = {
                "Authorization": f"Bearer {reg['token']}",
                "Content-Type": "application/json",
                "X-LemonCrow-Bridge": bridge_id,
            }
            if sid:
                headers["Mcp-Session-Id"] = sid
            if host:
                headers["X-LemonCrow-Agent"] = host
            try:
                resp = client.post(f"http://127.0.0.1:{reg['port']}/mcp", headers=headers, content=body)
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectTimeout):
                if attempt == 0:
                    handle.respawn(reg)  # daemon died/reaped -> respawn and retry once
                    continue
                return _jsonrpc_error(request_id, -32000, "MCP daemon unreachable")
            if resp.status_code == 202:
                return None  # notification: no response frame
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    return _jsonrpc_error(request_id, -32603, "invalid daemon response")
            if resp.status_code in (401, 403) and attempt == 0:
                handle.respawn(reg)  # token rotated by a respawn between calls
                continue
            return _jsonrpc_error(request_id, -32000, f"MCP daemon HTTP {resp.status_code}")
        return _jsonrpc_error(request_id, -32000, "MCP daemon unreachable")

    def _forward(line: str) -> None:
        try:
            req = json.loads(line)
        except (json.JSONDecodeError, RecursionError) as exc:
            _write(_jsonrpc_error(None, -32700, f"parse error: {type(exc).__name__}"))
            return
        if not isinstance(req, dict):
            _write(_jsonrpc_error(None, -32600, "invalid request: expected a JSON object"))
            return
        try:
            response = _post(req)
        except Exception as exc:
            _log.exception("MCP bridge forward failed")
            response = _jsonrpc_error(req.get("id"), -32603, f"bridge error: {exc}")
        if response is not None:
            _write(response)

    stop = threading.Event()

    def _ping_loop() -> None:
        # Keep this session attached so the daemon's idle reaper never tears it
        # down mid-session. Best-effort: a failed ping means the daemon crashed,
        # in which case the next real request respawns it -- an idle bridge must
        # NOT resurrect a daemon that legitimately reaped itself.
        while not stop.wait(_PING_INTERVAL_SECONDS):
            reg = handle.current()
            try:
                client.post(
                    f"http://127.0.0.1:{reg['port']}/session/ping",
                    headers={"Authorization": f"Bearer {reg['token']}", "X-LemonCrow-Bridge": bridge_id},
                    timeout=5.0,
                )
            except Exception:
                _log.debug("bridge ping failed", exc_info=True)

    threading.Thread(target=_ping_loop, daemon=True, name="mcp-bridge-ping").start()

    pool = ThreadPoolExecutor(max_workers=_MAX_INFLIGHT, thread_name_prefix="mcp-bridge")
    try:
        for line in sys.stdin:
            line = line.strip()
            if line:
                pool.submit(_forward, line)
    finally:
        # Host closed stdin: detach promptly so the daemon can reap this repo
        # once no other sessions remain, then drain in-flight forwards.
        stop.set()
        reg = handle.current()
        try:
            client.post(
                f"http://127.0.0.1:{reg['port']}/session/close",
                headers={"Authorization": f"Bearer {reg['token']}", "X-LemonCrow-Bridge": bridge_id},
                timeout=5.0,
            )
        except Exception:
            _log.debug("bridge close failed", exc_info=True)
        pool.shutdown(wait=True, cancel_futures=False)
        client.close()


def singleton_enabled() -> bool:
    """Whether ``lc mcp`` should run the singleton bridge instead of legacy stdio.

    Default ON: one shared per-workspace daemon replaces N heavy stdio servers.
    Opt out per host/session with ``LEMONCROW_MCP_SINGLETON=0`` (also: false/no/off)
    to fall back to the legacy one-process-per-session stdio server.
    """
    return os.environ.get("LEMONCROW_MCP_SINGLETON", "1").strip().lower() not in ("0", "false", "no", "off")
