"""The MCP stdio entry point. One process, one namespace, no daemon.

It is a child of the host session: it reads JSON-RPC from stdin, writes to
stdout, and exits when stdin closes. There is no port, no pid file, no socket,
no unit, no ``atexit`` respawn and no idle reaper -- the list from the design's
daemon disposition, in the negative.

The advertised surface is the enterprise-safe subset of the public LemonCrow
registry. ``mcp``, ``agent`` and ``workflow`` are intentionally absent because
their legacy local implementations start another MCP/LemonCrow process. Where
an advertised tool *runs* is decided by :mod:`lemoncrow_client.routing` and is
invisible from here.

Bootstrap belongs to this process and is fail-open. MCP ``initialize`` opens
the remote session/view and holds it until stdin closes. If bootstrap fails, the
reason is recorded and client-side tools keep working; a later server-dependent
tool retries bootstrap after a short cooldown, so a local server that finishes
starting does not leave indexing disabled for the lifetime of the host session.
A stdio process that refused to initialize because a remote service was down
would strand the developer -- which is the failure mode the whole design exists
to remove.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Mapping
from typing import Any, Final, TextIO

from . import CLIENT_VERSION, toolbroker
from .config import ClientConfig, load_config
from .dispatcher import Dispatcher
from .errors import ClientError, ErrorCode
from .routing import Site, route_for
from .search_dedup import SearchResultDedup
from .session import BootstrapReport, RemoteSession
from .surface import ToolSpec, tool_list

__all__ = ["McpServer", "serve"]

#: The MCP protocol version this client speaks to the host.
MCP_PROTOCOL_VERSION: Final[str] = "2025-06-18"

SERVER_NAME: Final[str] = "lc"
_MAX_LINE_BYTES: Final[int] = 32 * 1024 * 1024
_OFFLINE_RETRY_INTERVAL_S: Final[float] = 1.0

_INSTRUCTIONS: Final[str] = (
    "LemonCrow thin client. This one stdio process owns the LemonCrow session for its lifetime; "
    "it starts no LemonCrow daemon, local server, MCP child server or second LemonCrow executable. "
    "Local tools are bash, edit, grep, blame, scan, codemod and sql; indexed/intelligence tools run "
    "on the configured LemonCrow server. A degraded result says why, and a refusal carries the next step."
)


class McpServer:
    """One JSON-RPC-over-stdio session."""

    _bootstrap_report: BootstrapReport | None
    __slots__ = (
        "_bootstrap_report",
        "_config",
        "_dispatcher",
        "_initialized",
        "_last_bootstrap_attempt",
        "_search_dedup",
        "_session",
        "_tools",
    )

    def __init__(self, config: ClientConfig, session: RemoteSession | None = None) -> None:
        self._config = config
        self._session = session if session is not None else RemoteSession(config)
        self._dispatcher = Dispatcher(config, self._session)
        self._search_dedup = SearchResultDedup(config.repo_root)
        self._tools = _advertised_tools()
        self._initialized = False
        self._bootstrap_report = None
        self._last_bootstrap_attempt = 0.0

    @property
    def dispatcher(self) -> Dispatcher:
        return self._dispatcher

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        """Answer one request, or ``None`` for a notification."""
        method = str(request.get("method") or "")
        request_id = request.get("id")
        params = request.get("params")
        arguments: Mapping[str, Any] = params if isinstance(params, Mapping) else {}

        if method == "initialize":
            self._search_dedup.clear()
            self._initialized = True
            self._ensure_session()
            return _ok(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "title": "LemonCrow",
                        "version": CLIENT_VERSION,
                        "description": (
                            "Indexed code search, bounded file tools, editing and verification "
                            "for coding agents, served by the LemonCrow server."
                        ),
                    },
                    "capabilities": {"tools": {}},
                    "instructions": f"{_INSTRUCTIONS} {self._session_status()}",
                },
            )
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return _ok(request_id, {})
        if method == "tools/list":
            return _ok(request_id, {"tools": [dict(spec) for spec in self._tools]})
        if method == "tools/call":
            return self._call(request_id, arguments)
        if request_id is None:
            return None
        return _err(request_id, -32601, f"unknown method: {method}")

    def _call(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        raw_arguments = params.get("arguments")
        if isinstance(raw_arguments, str):
            # Some hosts deliver the whole arguments object as a JSON string.
            try:
                raw_arguments = json.loads(raw_arguments)
            except (ValueError, RecursionError):
                raw_arguments = {}
        arguments: Mapping[str, Any] = raw_arguments if isinstance(raw_arguments, Mapping) else {}
        if name == toolbroker.BROKER_TOOL_NAME and toolbroker.broker_enabled():
            # The broker's own target may be server-sited; always worth a retry
            # since these calls are rare and the bootstrap check is cheap once
            # already bootstrapped.
            self._ensure_session(retry=True)
            outcome = toolbroker.handle(arguments, dispatcher=self._dispatcher)
            return _ok(request_id, outcome.to_mcp())
        retry_bootstrap = False
        try:
            retry_bootstrap = route_for(name).site is not Site.CLIENT
        except ClientError:
            pass
        self._ensure_session(retry=retry_bootstrap)
        outcome = self._dispatcher.call(name, arguments)
        if name == "code_search":
            outcome = self._search_dedup.compact(
                outcome,
                arguments=arguments,
                session_id=self._session.state.session_id,
                view_id=self._session.state.view_id,
            )
        # stdio MCP is the model-facing surface. ToolOutcome keeps structured
        # data internally for caches/hydration/programmatic callers, but this
        # boundary deliberately emits only the compact rendered content.
        return _ok(request_id, outcome.to_mcp())

    def _ensure_session(self, *, retry: bool = False) -> None:
        """Open this process's remote session, retrying after startup races."""
        if self._session.bootstrapped:
            return
        now = time.monotonic()
        if self._session.state.reason:
            if not retry or now - self._last_bootstrap_attempt < _OFFLINE_RETRY_INTERVAL_S:
                return
        self._last_bootstrap_attempt = now
        try:
            report = self._session.bootstrap(deadline_s=self._config.startup_budget_s)
        except ClientError as exc:  # pragma: no cover - bootstrap is fail-open
            self._session.state.go_offline(f"{exc.server_code}: {exc.message}")
            return
        self._bootstrap_report = report
        if not report.ok:
            self._session.state.go_offline(report.reason)

    def _session_status(self) -> str:
        report = self._bootstrap_report
        if report is not None:
            return report.one_line(self._config.url)
        reason = self._session.state.reason
        if reason:
            return f"LemonCrow: server-side tools unavailable ({reason}); client-side tools still work"
        return "LemonCrow: session bootstrap pending"

    def close(self) -> None:
        self._search_dedup.clear()
        self._session.close()


def _advertised_tools() -> list[ToolSpec]:
    """The full ``tools/list`` payload: routed tools, plus the broker if active."""
    tools = tool_list()
    if toolbroker.broker_enabled():
        tools = sorted([*tools, ToolSpec(toolbroker.broker_spec())], key=lambda spec: str(spec["name"]))
    return tools


def serve(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    config: ClientConfig | None = None,
) -> int:
    """Read requests until stdin closes, then exit. That is the whole lifecycle."""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    try:
        resolved = config if config is not None else load_config()
    except ClientError as exc:
        # A misconfiguration is fatal and must be visible. Writing a JSON-RPC
        # error would be answering a request nobody made.
        print(f"lemoncrow-client: {exc.message}", file=sys.stderr)
        return 2
    server = McpServer(resolved)
    try:
        for line in source:
            if not line.strip():
                continue
            if len(line) > _MAX_LINE_BYTES:
                _write(sink, _err(None, -32600, "request exceeds the client's line limit"))
                continue
            try:
                request = json.loads(line)
            except (ValueError, RecursionError):
                _write(sink, _err(None, -32700, "parse error"))
                continue
            if not isinstance(request, Mapping):
                _write(sink, _err(None, -32600, "request must be a JSON object"))
                continue
            response = server.handle(request)
            if response is not None:
                _write(sink, response)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    finally:
        server.close()
    return 0


def _write(sink: TextIO, payload: Mapping[str, Any]) -> None:
    sink.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sink.flush()


def _ok(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _err(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message, "data": {"client_code": ErrorCode.INTERNAL.value}},
    }
