"""Persistent remote MCP surface for the public loopback server.

Cloudflare forwards every configured connector hostname to the one local
LemonCrow server.  This surface keeps the OSS pairing-code OAuth authority
local and durable; hosted deployments use their separate Authward surface.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web
from lemoncrow_client.config import ClientConfig, load_config
from lemoncrow_client.mcpserver import McpServer

from lemoncrow.gateway.adapters.mcp_oauth import (
    _AUTHORIZE_FIELDS,
    ACCESS_TOKEN_TTL_SECONDS,
    _append_query,
    _error_page,
    _is_allowed_redirect_uri,
    _OAuthStore,
    _pkce_s256,
    _render_form,
    default_pairing_path,
    default_state_path,
    load_or_create_pairing_code,
    migrate_legacy_state,
)
from lemoncrow.gateway.mcp_connectors import ConnectorBinding, connector_slug, load_connector_for_hostname

_MCP_PATH = "/mcp"
_MAX_BODY = 4 * 1024 * 1024
_MAX_SESSIONS = 64
_REMOTE_WORKERS = 64
# Local remote MCP calls for one OAuth client are serialized. Long-running
# LemonCrow tools can legitimately hold that lane for minutes, so a short
# admission timeout turns normal single-user overlap into a misleading HTTP 429
# "busy/rate-limited" failure. Queue essentially indefinitely in local mode;
# the tool/request deadlines still bound the actual work once admitted.
_ADMISSION_WAIT_S = 24 * 60 * 60.0
_SESSION_IDLE_S = 3600.0
_DISCOVERY_TTL_S = 60.0

LOCAL_REMOTE_MCP_PUBLIC_PATHS = frozenset(
    {
        "/healthz",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/mcp",
        "/.well-known/openid-configuration",
        "/.well-known/openid-configuration/mcp",
        "/.well-known/mcp.json",
        "/register",
        "/authorize",
        "/token",
        _MCP_PATH,
    }
)


def _public_host(request: web.Request) -> str:
    forwarded = request.headers.get("x-forwarded-host", "")
    raw = forwarded.split(",", 1)[0].strip() if forwarded else request.host
    return raw.rsplit(":", 1)[0].lower()


def _public_base_url(request: web.Request) -> str:
    forwarded = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded.split(",", 1)[0].strip() if forwarded else request.scheme
    return f"{scheme}://{_public_host(request)}"


def _form(raw: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in urllib.parse.parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True):
        out.setdefault(key, value)
    return out


def _json_error(error: str, description: str, *, status: int = 400) -> web.Response:
    return web.json_response(
        {"error": error, "error_description": description},
        status=status,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@dataclass(slots=True)
class _ThinSession:
    server: McpServer
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = 0.0


class LocalRemoteMcpSurface:
    """Pairing-code OAuth and streamable HTTP MCP for local connectors."""

    def __init__(self, *, backend_port: int = 7420) -> None:
        self._backend_port = backend_port
        self._stores: dict[str, _OAuthStore] = {}
        self._sessions: dict[tuple[str, str], _ThinSession] = {}
        self._executor = ThreadPoolExecutor(max_workers=_REMOTE_WORKERS, thread_name_prefix="lc-local-remote-mcp")
        self._capacity = asyncio.Semaphore(_REMOTE_WORKERS)
        self._discovery: dict[str, tuple[float, list[dict[str, str]]]] = {}

    def route_defs(self) -> tuple[web.RouteDef, ...]:
        return (
            web.get("/.well-known/oauth-protected-resource", self.protected_resource),
            web.get("/.well-known/oauth-protected-resource/mcp", self.protected_resource),
            web.get("/.well-known/oauth-authorization-server", self.authorization_metadata),
            web.get("/.well-known/oauth-authorization-server/mcp", self.authorization_metadata),
            web.get("/.well-known/openid-configuration", self.authorization_metadata),
            web.get("/.well-known/openid-configuration/mcp", self.authorization_metadata),
            web.get("/.well-known/mcp.json", self.mcp_discovery),
            web.post("/register", self.register),
            web.get("/authorize", self.authorize_get),
            web.post("/authorize", self.authorize_post),
            web.post("/token", self.token),
            web.get(_MCP_PATH, self.mcp_get),
            web.post(_MCP_PATH, self.mcp_post),
        )

    @staticmethod
    def owns_host(request: web.Request) -> bool:
        return load_connector_for_hostname(_public_host(request)) is not None

    def _binding(self, request: web.Request) -> ConnectorBinding:
        binding = load_connector_for_hostname(_public_host(request))
        if binding is None:
            raise web.HTTPNotFound(text="unknown LemonCrow MCP connector hostname")
        return binding

    def _store(self, binding: ConnectorBinding) -> _OAuthStore:
        store = self._stores.get(binding.hostname)
        if store is None:
            scope = connector_slug(binding.hostname)
            migrate_legacy_state(scope)
            store = _OAuthStore(default_state_path(scope))
            self._stores[binding.hostname] = store
        return store

    def _pairing_code(self, binding: ConnectorBinding) -> str:
        scope = connector_slug(binding.hostname)
        migrate_legacy_state(scope)
        return load_or_create_pairing_code(default_pairing_path(scope))

    async def protected_resource(self, request: web.Request) -> web.Response:
        self._binding(request)
        base = _public_base_url(request)
        return web.json_response(
            {"resource": base, "authorization_servers": [base], "bearer_methods_supported": ["header"]},
            headers={"Cache-Control": "no-store"},
        )

    async def authorization_metadata(self, request: web.Request) -> web.Response:
        self._binding(request)
        base = _public_base_url(request)
        return web.json_response(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": [],
            },
            headers={"Cache-Control": "no-store"},
        )

    async def mcp_discovery(self, request: web.Request) -> web.Response:
        binding = self._binding(request)
        cached = self._discovery.get(binding.hostname)
        if cached is not None and time.monotonic() - cached[0] < _DISCOVERY_TTL_S:
            tools = cached[1]
        else:
            async with self._capacity:
                tools = await asyncio.get_running_loop().run_in_executor(self._executor, self._list_tools, binding)
            self._discovery[binding.hostname] = (time.monotonic(), tools)
        return web.json_response(
            {
                "name": "lc",
                "title": "LemonCrow",
                "transport": {"type": "streamable-http", "endpoint": _MCP_PATH},
                "capabilities": {"tools": {}},
                "tools": tools,
            }
        )

    def _list_tools(self, binding: ConnectorBinding) -> list[dict[str, str]]:
        server = McpServer(self._client_config(binding))
        try:
            listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        finally:
            server.close()
        tools = [] if not listed else list(listed.get("result", {}).get("tools", []))
        return [{"name": item.get("name", ""), "description": item.get("description", "")} for item in tools]

    async def register(self, request: web.Request) -> web.Response:
        binding = self._binding(request)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return _json_error("invalid_client_metadata", "request body is not valid JSON")
        if not isinstance(body, dict):
            return _json_error("invalid_client_metadata", "request body must be a JSON object")
        redirect_uris = body.get("redirect_uris")
        if (
            not isinstance(redirect_uris, list)
            or not redirect_uris
            or not all(isinstance(uri, str) for uri in redirect_uris)
        ):
            return _json_error(
                "invalid_redirect_uri", "redirect_uris is required and must be a non-empty list of strings"
            )
        for uri in redirect_uris:
            if not _is_allowed_redirect_uri(uri):
                return _json_error("invalid_redirect_uri", f"redirect_uri must be https (or http loopback): {uri}")
        grant_types = body.get("grant_types") or ["authorization_code", "refresh_token"]
        response_types = body.get("response_types") or ["code"]
        record = self._store(binding).register_client(
            redirect_uris=list(redirect_uris),
            client_name=body.get("client_name") if isinstance(body.get("client_name"), str) else None,
            grant_types=list(grant_types) if isinstance(grant_types, list) else ["authorization_code", "refresh_token"],
            response_types=list(response_types) if isinstance(response_types, list) else ["code"],
        )
        return web.json_response(record, status=201)

    async def authorize_get(self, request: web.Request) -> web.Response:
        binding = self._binding(request)
        query = request.query
        client = self._store(binding).get_client(query.get("client_id", ""))
        if client is None:
            return web.Response(text=_error_page("Unknown client_id."), content_type="text/html", status=400)
        redirect_uri = query.get("redirect_uri", "")
        if redirect_uri not in client["redirect_uris"]:
            return web.Response(
                text=_error_page("redirect_uri does not match a registered value."),
                content_type="text/html",
                status=400,
            )
        if query.get("response_type", "") != "code":
            return web.Response(text=_error_page("response_type must be 'code'."), content_type="text/html", status=400)
        if query.get("code_challenge_method", "") != "S256" or not query.get("code_challenge", ""):
            return web.Response(
                text=_error_page("PKCE with code_challenge_method=S256 is required."),
                content_type="text/html",
                status=400,
            )
        params = {key: query.get(key, "") for key in _AUTHORIZE_FIELDS}
        return web.Response(text=_render_form(params, error=None), content_type="text/html")

    async def authorize_post(self, request: web.Request) -> web.Response:
        binding = self._binding(request)
        store = self._store(binding)
        form = _form(await request.read())
        client = store.get_client(form.get("client_id", ""))
        redirect_uri = form.get("redirect_uri", "")
        if client is None:
            return web.Response(text=_error_page("Unknown client_id."), content_type="text/html", status=400)
        if redirect_uri not in client["redirect_uris"]:
            return web.Response(
                text=_error_page("redirect_uri does not match a registered value."),
                content_type="text/html",
                status=400,
            )
        params = {key: form.get(key, "") for key in _AUTHORIZE_FIELDS}
        remaining = store.pairing_lockout_remaining()
        if remaining > 0:
            return web.Response(
                text=_render_form(params, error=f"Too many attempts. Try again in {int(remaining) + 1}s."),
                content_type="text/html",
                status=429,
            )
        submitted = form.get("pairing_code", "")
        if not (submitted and hmac.compare_digest(submitted, self._pairing_code(binding))):
            store.record_pairing_failure()
            return web.Response(text=_render_form(params, error="Incorrect pairing code."), content_type="text/html")
        store.reset_pairing_failures()
        code = store.create_auth_code(
            client_id=form.get("client_id", ""),
            redirect_uri=redirect_uri,
            code_challenge=params["code_challenge"],
            resource=params["resource"],
        )
        raise web.HTTPFound(_append_query(redirect_uri, {"code": code, "state": params["state"]}))

    async def token(self, request: web.Request) -> web.Response:
        store = self._store(self._binding(request))
        form = _form(await request.read())
        grant_type = form.get("grant_type", "")
        if grant_type == "authorization_code":
            code = form.get("code", "")
            client_id = form.get("client_id", "")
            redirect_uri = form.get("redirect_uri", "")
            verifier = form.get("code_verifier", "")
            if not code or not verifier:
                return _json_error("invalid_request", "code and code_verifier are required")
            record = store.consume_auth_code(code)
            if record is None:
                return _json_error("invalid_grant", "authorization code is invalid or expired")
            if record["client_id"] != client_id:
                return _json_error("invalid_grant", "client_id does not match the authorization code")
            if record["redirect_uri"] != redirect_uri:
                return _json_error("invalid_grant", "redirect_uri does not match the authorization code")
            if not hmac.compare_digest(_pkce_s256(verifier), str(record["code_challenge"])):
                return _json_error("invalid_grant", "PKCE verification failed")
            return self._token_success(*store.issue_tokens(client_id))
        if grant_type == "refresh_token":
            refresh = form.get("refresh_token", "")
            if not refresh:
                return _json_error("invalid_request", "refresh_token is required")
            rotated = store.rotate_refresh_token(refresh)
            if rotated is None:
                return _json_error("invalid_grant", "refresh_token is invalid or already used")
            return self._token_success(*rotated)
        return _json_error("unsupported_grant_type", f"unsupported grant_type: {grant_type!r}")

    @staticmethod
    def _token_success(access: str, refresh: str) -> web.Response:
        return web.json_response(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL_SECONDS,
                "refresh_token": refresh,
                "scope": "",
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    def _bearer(self, request: web.Request, binding: ConnectorBinding) -> tuple[str, web.Response | None]:
        scheme, _, raw = request.headers.get("authorization", "").partition(" ")
        client_id = (
            self._store(binding).access_token_client_id(raw.strip())
            if scheme.lower() == "bearer" and raw.strip()
            else None
        )
        if client_id is not None:
            return client_id, None
        base = _public_base_url(request)
        return "", web.json_response(
            {"error": "unauthorized", "error_description": "missing or invalid access token"},
            status=401,
            headers={
                "Cache-Control": "no-store",
                "WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"',
            },
        )

    def _client_config(self, binding: ConnectorBinding) -> ClientConfig:
        env = dict(os.environ)
        env["LEMONCROW_URL"] = f"http://127.0.0.1:{self._backend_port}"
        env["LEMONCROW_INSTALL_MODE"] = "local"
        env["LEMONCROW_LOCAL_FS"] = "1"
        env.setdefault("LEMONCROW_STARTUP_BUDGET_S", "30")
        env.setdefault("LEMONCROW_REQUEST_TIMEOUT_S", "30")
        return load_config(env, cwd=Path(binding.workspace))

    async def _session(self, binding: ConnectorBinding, client_id: str) -> _ThinSession | None:
        key = (binding.hostname, client_id)
        now = time.monotonic()
        session = self._sessions.get(key)
        if session is not None:
            session.last_used = now
            return session
        stale = [
            held_key
            for held_key, held in self._sessions.items()
            if not held.lock.locked() and now - held.last_used > _SESSION_IDLE_S
        ]
        while len(self._sessions) - len(stale) >= _MAX_SESSIONS:
            candidates = [
                (held.last_used, held_key)
                for held_key, held in self._sessions.items()
                if held_key not in stale and not held.lock.locked()
            ]
            if not candidates:
                return None
            stale.append(min(candidates)[1])
        loop = asyncio.get_running_loop()
        for held_key in stale:
            held = self._sessions.pop(held_key, None)
            if held is not None:
                loop.run_in_executor(self._executor, held.server.close)
        session = _ThinSession(McpServer(self._client_config(binding)), last_used=now)
        self._sessions[key] = session
        return session

    @staticmethod
    def _busy(request_id: object) -> web.Response:
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": "LemonCrow local MCP admission wait expired; retry this request",
                    "data": {"retryable": True},
                },
            },
            status=429,
            headers={"Retry-After": "1", "Cache-Control": "no-store"},
        )

    async def mcp_post(self, request: web.Request) -> web.StreamResponse:
        binding = self._binding(request)
        client_id, refusal = self._bearer(request, binding)
        if refusal is not None:
            return refusal
        raw = await request.read()
        if len(raw) > _MAX_BODY:
            raise web.HTTPRequestEntityTooLarge(max_size=_MAX_BODY, actual_size=len(raw))
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError, RecursionError):
            return web.json_response(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            )
        if not isinstance(body, dict):
            return web.json_response(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
            )
        session = await self._session(binding, client_id)
        if session is None:
            return self._busy(body.get("id"))
        # lc-debt: calls for one OAuth client are serialized; port the hosted
        # lane admission model if parallel local MCP calls become necessary.
        lock_acquired = False
        capacity_acquired = False
        try:
            await asyncio.wait_for(session.lock.acquire(), timeout=_ADMISSION_WAIT_S)
            lock_acquired = True
            await asyncio.wait_for(self._capacity.acquire(), timeout=_ADMISSION_WAIT_S)
            capacity_acquired = True
        except TimeoutError:
            if lock_acquired:
                session.lock.release()
            return self._busy(body.get("id"))
        try:
            result = await asyncio.get_running_loop().run_in_executor(self._executor, session.server.handle, body)
        finally:
            if capacity_acquired:
                self._capacity.release()
            if lock_acquired:
                session.lock.release()
            session.last_used = time.monotonic()
        if result is None:
            return web.json_response(None, status=202)
        if "text/event-stream" in request.headers.get("accept", "").lower():
            payload = f"data: {json.dumps(result, ensure_ascii=True)}\n\n".encode()
            return web.Response(body=payload, content_type="text/event-stream")
        return web.json_response(result)

    async def mcp_get(self, request: web.Request) -> web.StreamResponse:
        binding = self._binding(request)
        _client_id, refusal = self._bearer(request, binding)
        if refusal is not None:
            return refusal
        return web.Response(
            status=405,
            text="remote MCP is stateless; use POST /mcp",
            headers={"Allow": "POST", "Cache-Control": "no-store"},
        )

    def close(self) -> None:
        sessions = tuple(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            self._executor.submit(session.server.close)
        self._executor.shutdown(wait=False, cancel_futures=True)
