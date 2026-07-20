"""Streamable-HTTP / SSE MCP transport for LemonCrow (G17).

This is an *opt-in*, additive transport that runs alongside the default stdio
MCP server. It reuses the exact JSON-RPC dispatcher (``mcp_server._handle``) and
tool registry (``mcp_server.TOOLS``) so every transport exposes identical
behavior: ``initialize``, ``tools/list``, and ``tools/call`` all flow through
the same code path.

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
import re
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from lemoncrow.gateway.adapters import mcp_server

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
    """The advertised tool surface, filtered by the shared visibility policy."""
    return [
        {
            "name": name,
            "description": mcp_server._tool_description(spec),
            "inputSchema": spec.get("inputSchema", {}),
        }
        for name, spec in mcp_server.TOOLS.items()
        if mcp_server._tool_visible_to_llm(name, spec)
    ]


def discovery_manifest(*, endpoint: str = MCP_HTTP_PATH) -> dict[str, Any]:
    """Build the ``.well-known/mcp.json`` discovery document.

    Advertises the server identity, the streamable-HTTP endpoint, the protocol
    version, and the public tool names so a client can discover LemonCrow without
    a round-trip handshake.
    """
    tools = _public_tools()
    return {
        "name": mcp_server.SERVER_NAME,
        "version": mcp_server.SERVER_VERSION,
        "protocolVersion": mcp_server.PROTOCOL_VERSION,
        "transport": {
            "type": "streamable-http",
            "endpoint": endpoint,
        },
        "capabilities": {"tools": {}},
        "tools": [{"name": tool["name"], "description": tool["description"]} for tool in tools],
    }


# Strip server filesystem paths from error text while preserving relative paths
# and URLs (which a remote agent needs to self-correct). Match POSIX absolute
# paths only at a boundary (not preceded by a word char or ``/``, so ``https://``
# and ``src/lemoncrow/foo.py`` are left intact) plus Windows drive paths.
_ABS_PATH_RE = re.compile(r"(?<![\w/])(?:/[\w.+\-]+)+/?|[A-Za-z]:\\[^\s:*?\"<>|]+")


def _redact_error_paths(response: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip absolute filesystem paths from a JSON-RPC error message before it
    crosses the HTTP boundary. _handle returns tool errors as ``str(exc)``, which
    can embed server paths; keep the error code + human-readable reason, drop the
    leaked paths (so a remote agent can still self-correct on the reason)."""
    if isinstance(response, dict):
        error = response.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            error["message"] = _ABS_PATH_RE.sub("<path>", error["message"])
    return response


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
    prior_ledger = mcp_server._set_request_ledger(session_id)
    prior_session = mcp_server._set_request_session(session_id or "", host or "", "", bridge_id or "")
    try:
        response = mcp_server._handle(request_obj)
        if isinstance(response, mcp_server._Deferred):
            # Deferral is armed only on the stdio server worker path
            # (_handle_and_write); the HTTP adapter never sets that context, so a
            # deferred marker is unreachable here. Guard for type safety.
            return mcp_server._err(request_obj.get("id"), -32603, "internal error: unexpected deferred result")
        return response
    except Exception:
        correlation_id = uuid.uuid4().hex
        logger.exception("MCP HTTP dispatch failed (correlation_id=%s)", correlation_id)
        return mcp_server._err(
            request_obj.get("id"),
            -32603,
            f"internal error (correlation_id={correlation_id})",
        )
    finally:
        mcp_server._clear_request_session(prior_session)
        mcp_server._clear_request_ledger(prior_ledger)


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
) -> FastAPI:
    """Mount the MCP HTTP/SSE transport and discovery manifest onto ``app``.

    Additive: registers new routes only; existing routes are untouched.

    C1 — ``auth_dependency`` (when provided) gates the POST/GET ``/mcp`` routes
    with the same FastAPI dependency the ``/v1/*`` routes use, so the tool surface
    is not reachable unauthenticated while the rest of the gateway is locked down.
    The discovery manifest stays public (it advertises only public tool names).
    """
    route_deps = [Depends(auth_dependency)] if auth_dependency is not None else []

    @app.get(MCP_DISCOVERY_PATH)
    async def mcp_discovery() -> dict[str, Any]:
        return discovery_manifest(endpoint=path)

    @app.post(path, dependencies=route_deps)
    async def mcp_post(request: Request) -> Any:
        raw = await _read_capped_body(request, _max_body_bytes())
        if raw is None:
            return JSONResponse(
                mcp_server._err(None, -32600, "request body too large"),
                status_code=413,
            )
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError, RecursionError):
            # JSON-RPC over HTTP intentionally returns 200 with a JSON-RPC error
            # body (test_parse_error_returns_jsonrpc_error), so keep the status;
            # just don't echo the parser's exception text back to the client.
            return JSONResponse(mcp_server._err(None, -32700, "parse error: request body is not valid JSON"))
        if not isinstance(body, dict):
            return JSONResponse(mcp_server._err(None, -32600, "invalid request: expected a JSON object"))

        # H2 — _dispatch runs the synchronous shared handler; offload it to a
        # worker thread so a slow tool call cannot block the event loop. Pass the
        # client's MCP session id (F1) so its ledger is scoped per session.
        session_id = request.headers.get("mcp-session-id")
        host = request.headers.get("x-lemoncrow-agent")
        bridge_id = request.headers.get("x-lemoncrow-bridge")
        response = await run_in_threadpool(_dispatch, body, session_id, host, bridge_id)
        # F2 — strip leaked server paths from any error message at the boundary.
        response = _redact_error_paths(response)
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


def create_mcp_http_app(*, path: str = MCP_HTTP_PATH) -> FastAPI:
    """Build a standalone FastAPI app exposing only the MCP HTTP transport."""
    app = FastAPI(
        title="LemonCrow MCP (HTTP)",
        version=mcp_server.SERVER_VERSION,
        description="Streamable-HTTP / SSE MCP transport for LemonCrow.",
    )
    return register_mcp_http(app, path=path)
