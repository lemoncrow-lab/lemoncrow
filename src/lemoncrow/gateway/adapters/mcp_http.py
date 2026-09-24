"""Streamable-HTTP / SSE MCP transport for LemonCrow (G17).

This is an *opt-in*, additive transport that runs alongside the default stdio
MCP server. It uses the same canonical control handling, tool registry, and
transport-neutral tool runtime as stdio without importing the legacy MCP
composition module.

Endpoints:
  - ``POST /mcp``               — streamable-HTTP MCP: a single JSON-RPC request
                                  in, a JSON-RPC response out. When the client
                                  sends ``Accept: text/event-stream`` the same
                                  response is delivered as a one-shot SSE event.
  - ``GET  /mcp``               — opens an SSE channel (heartbeat keep-alive).
  - ``GET  /.well-known/mcp.json`` — discovery manifest (server + tool surface).

Nothing here changes ``serve()``; stdio stays the default. Mount this only when
HTTP is explicitly enabled.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse

from lemoncrow.gateway.adapters.mcp.control import handle_control_request
from lemoncrow.gateway.adapters.mcp.jsonrpc import error as jsonrpc_error
from lemoncrow.gateway.adapters.mcp.jsonrpc import ok as jsonrpc_ok
from lemoncrow.gateway.adapters.mcp.ledger import (
    _clear_request_ledger,
    _clear_request_session,
    _set_request_ledger,
    _set_request_session,
)
from lemoncrow.gateway.adapters.mcp_branding import ICON_BYTES, ICON_MIME_TYPE, ICON_PATH, icon_metadata
from lemoncrow.gateway.tools.call_runtime import execute_default_tool_payload
from lemoncrow.gateway.tools.errors import ToolProtocolError
from lemoncrow.gateway.tools.registry import advertised_tools, registered_tools
from lemoncrow.gateway.tools.surface import (
    MCP_PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    tool_description,
    tool_visible_to_llm,
)

logger = logging.getLogger(__name__)

MCP_HTTP_PATH = "/mcp"
MCP_DISCOVERY_PATH = "/.well-known/mcp.json"

# H2 — cap the request body BEFORE parsing so a hostile/oversized payload can't
# blow up memory or the event loop. Override with LEMONCROW_MCP_HTTP_MAX_BODY_BYTES.
_DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024


def _max_body_bytes() -> int:
    raw = os.environ.get("LEMONCROW_MCP_HTTP_MAX_BODY_BYTES", str(_DEFAULT_MAX_BODY_BYTES))
    try:
        configured = int(raw)
    except ValueError:
        logger.warning("invalid LEMONCROW_MCP_HTTP_MAX_BODY_BYTES=%r; using %d", raw, _DEFAULT_MAX_BODY_BYTES)
        return _DEFAULT_MAX_BODY_BYTES
    return max(64 * 1024, configured)


def _public_tools() -> list[dict[str, Any]]:
    """The canonical advertised tool surface for HTTP discovery."""
    return advertised_tools()


def discovery_manifest(*, endpoint: str = MCP_HTTP_PATH, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build the ``.well-known/mcp.json`` discovery document.

    Advertises the server identity, the streamable-HTTP endpoint, the protocol
    version, and the public tool names so a client can discover LemonCrow without
    a round-trip handshake.
    """
    tools = _public_tools() if tools is None else tools
    return {
        "name": SERVER_NAME,
        "title": "LemonCrow",
        "version": SERVER_VERSION,
        "description": "Indexed code search and bounded coding-agent tools.",
        "icons": [icon_metadata()],
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "transport": {
            "type": "streamable-http",
            "endpoint": endpoint,
        },
        "capabilities": {"tools": {}},
        "tools": [{"name": tool["name"], "description": tool["description"]} for tool in tools],
    }


def _dispatch_jsonrpc(request_obj: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch one synchronous HTTP JSON-RPC request through canonical runtime seams."""
    rid = request_obj.get("id")
    method = request_obj.get("method")
    params = request_obj.get("params") or {}

    control = handle_control_request(
        method,
        rid,
        # HTTP sessions are supplied explicitly by request headers; unlike the
        # stdio host this transport must not register itself as a window-local
        # MCP process during initialize.
        on_session_start=lambda: None,
        visibility=lambda name, _spec: tool_visible_to_llm(name),
        description=tool_description,
    )
    if control.handled:
        return control.response

    if method == "tools/call":
        # Bootstrap handler registration + the default transport-neutral runtime
        # before a direct tools/call that did not first perform tools/list.
        registered_tools()
        try:
            payload = execute_default_tool_payload(rid, params)
        except ToolProtocolError as exc:
            return jsonrpc_error(rid, exc.code, str(exc))
        return jsonrpc_ok(rid, payload)

    return jsonrpc_error(rid, -32601, f"unknown method: {method}")


# NOTE: error messages intentionally keep absolute filesystem paths. The daemon
# serves loopback/Unix-socket clients on the same machine, and self-correction
# hints (spill files, ``new_file`` retry paths) ARE absolute paths -- redacting
# them turned actionable errors into dead ends ("new_file <path> could not be
# read: ... '<path>'") while normal tool results already carry the same paths.


def _dispatch(
    request_obj: dict[str, Any],
    session_id: str | None = None,
    host: str | None = None,
    bridge_id: str | None = None,
) -> dict[str, Any] | None:
    """Run one JSON-RPC request through the shared dispatcher (fail-safe).

    M2 — a raw ``str(exc)`` leaks internals (paths, types, partial state) to the
    client. Log the full traceback server-side and return a generic message with
    a correlation id so an operator can still tie the client report to the log.

    F1 — scope the ledger to the client's ``Mcp-Session-Id`` (set on this worker
    thread, the one that runs _handle) so concurrent HTTP clients don't co-mingle
    into the process-global ledger. The same session id + the ``X-LemonCrow-Agent``
    host label are stamped as the request session context so every other
    session-id/host consumer inside _handle resolves the calling session (the
    per-workspace daemon cannot self-resolve its callers' windows).
    """
    prior_ledger = _set_request_ledger(session_id)
    prior_session = _set_request_session(session_id or "", host or "", "", bridge_id or "")
    try:
        return _dispatch_jsonrpc(request_obj)
    except Exception:
        correlation_id = uuid.uuid4().hex
        logger.exception("MCP HTTP dispatch failed (correlation_id=%s)", correlation_id)
        return jsonrpc_error(
            request_obj.get("id"),
            -32603,
            f"internal error (correlation_id={correlation_id})",
        )
    finally:
        _clear_request_session(prior_session)
        _clear_request_ledger(prior_ledger)


def _sse_event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _read_capped_body(request: Request, limit: int) -> bytes | None:
    """Read the request body, rejecting anything over ``limit`` bytes.

    H2 — check the declared ``Content-Length`` first (cheap reject), then cap the
    streamed read so a lying/absent header can't smuggle an oversized payload.
    Returns ``None`` when the body exceeds the cap.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None
        except ValueError:
            pass
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def register_mcp_http(
    app: FastAPI,
    *,
    path: str = MCP_HTTP_PATH,
    auth_dependency: Callable[..., Any] | None = None,
    dispatch: Callable[[dict[str, Any], str | None, str | None, str | None], dict[str, Any] | None] | None = None,
    tools_provider: Callable[[], list[dict[str, Any]]] | None = None,
) -> FastAPI:
    """Mount the MCP HTTP/SSE transport and discovery manifest onto ``app``.

    Additive: registers new routes only; existing routes are untouched.

    C1 — ``auth_dependency`` (when provided) gates the POST/GET ``/mcp`` routes
    with the same FastAPI dependency the ``/v1/*`` routes use, so the tool surface
    is not reachable unauthenticated while the rest of the gateway is locked down.
    The discovery manifest stays public (it advertises only public tool names).
    """
    route_deps = [Depends(auth_dependency)] if auth_dependency is not None else []

    @app.get(ICON_PATH, include_in_schema=False)
    @app.get("/favicon.ico", include_in_schema=False)
    async def mcp_icon() -> Response:
        return Response(
            content=ICON_BYTES,
            media_type=ICON_MIME_TYPE,
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    @app.get(MCP_DISCOVERY_PATH)
    async def mcp_discovery() -> dict[str, Any]:
        tools = tools_provider() if tools_provider is not None else _public_tools()
        return discovery_manifest(endpoint=path, tools=tools)

    @app.post(path, dependencies=route_deps)
    async def mcp_post(request: Request) -> Any:
        raw = await _read_capped_body(request, _max_body_bytes())
        if raw is None:
            return JSONResponse(
                jsonrpc_error(None, -32600, "request body too large"),
                status_code=413,
            )
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError, RecursionError):
            # JSON-RPC over HTTP intentionally returns 200 with a JSON-RPC error
            # body (test_parse_error_returns_jsonrpc_error), so keep the status;
            # just don't echo the parser's exception text back to the client.
            return JSONResponse(jsonrpc_error(None, -32700, "parse error: request body is not valid JSON"))
        if not isinstance(body, dict):
            return JSONResponse(jsonrpc_error(None, -32600, "invalid request: expected a JSON object"))

        # H2 — _dispatch runs the synchronous shared handler; offload it to a
        # worker thread so a slow tool call cannot block the event loop. Pass the
        # client's MCP session id (F1) so its ledger is scoped per session.
        session_id = request.headers.get("mcp-session-id")
        host = request.headers.get("x-lemoncrow-agent")
        bridge_id = request.headers.get("x-lemoncrow-bridge")
        dispatcher = dispatch or _dispatch
        response = await run_in_threadpool(dispatcher, body, session_id, host, bridge_id)
        accept = request.headers.get("accept", "")
        wants_sse = "text/event-stream" in accept.lower()

        if not wants_sse:
            # Notifications (e.g. notifications/initialized) yield no response.
            if response is None:
                return JSONResponse(content=None, status_code=202)
            return JSONResponse(response)

        async def _one_shot() -> AsyncIterator[str]:
            if response is not None:
                yield _sse_event(response)

        return StreamingResponse(_one_shot(), media_type="text/event-stream")

    @app.get(path, dependencies=route_deps)
    async def mcp_get() -> StreamingResponse:
        async def _open_stream() -> AsyncIterator[str]:
            # Minimal keep-alive SSE channel. Server-initiated messages are not
            # used by LemonCrow's tool surface; the heartbeat keeps the standard
            # GET-SSE handshake satisfied for clients that probe it.
            yield ": mcp-stream-open\n\n"

        return StreamingResponse(_open_stream(), media_type="text/event-stream")

    return app


def create_mcp_http_app(
    *,
    path: str = MCP_HTTP_PATH,
    dispatch: Callable[[dict[str, Any], str | None, str | None, str | None], dict[str, Any] | None] | None = None,
    tools_provider: Callable[[], list[dict[str, Any]]] | None = None,
) -> FastAPI:
    """Build a standalone FastAPI app exposing only the MCP HTTP transport."""
    app = FastAPI(
        title="LemonCrow MCP (HTTP)",
        version=SERVER_VERSION,
        description="Streamable-HTTP / SSE MCP transport for LemonCrow.",
    )
    return register_mcp_http(app, path=path, dispatch=dispatch, tools_provider=tools_provider)
