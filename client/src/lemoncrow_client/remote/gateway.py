"""Pairing-code OAuth plus streamable-HTTP MCP, on one loopback listener.

A tunnel forwards every configured connector hostname here. The hostname picks
a durable workspace binding (:mod:`.state`); the bearer picks the OAuth client;
the pair keys one MCP session whose backend is chosen by the Runtime install
mode:

* Lite (the default): :class:`lemoncrow_client.lite.mcp.LiteMcp` for the
  session's Git worktree, in this process. No LemonCrow server exists.
* hosted: :class:`lemoncrow_client.mcpserver.McpServer`, the thin client.
  Client-site tools run here; server-site tools go to the hosted URL.

Admission mirrors the server gateway this replaces: control methods answer
immediately; stateless client-site tools share a small concurrent lane; every
other call is serialized per session.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, Protocol

from ..errors import ClientError
from ..routing import Site, route_for
from .state import (
    ACCESS_TOKEN_TTL_SECONDS,
    AUTHORIZE_FIELDS,
    ConnectorBinding,
    OAuthStore,
    append_query,
    connector_slug,
    consume_pairing_code,
    current_pairing_code,
    error_page,
    is_allowed_redirect_uri,
    load_connector_for_hostname,
    migrate_legacy_state,
    pairing_path,
    pkce_s256,
    render_form,
    resolve_registered_project,
    state_path,
    store_root_from,
)

MCP_PATH: Final[str] = "/mcp"
_MAX_BODY: Final[int] = 4 * 1024 * 1024
_MAX_SESSIONS: Final[int] = 64
_WORKERS: Final[int] = 64
# One client's stateful calls are serialized; long tools may legitimately hold
# that lane for minutes, so admission waits essentially indefinitely.
_SERIAL_ADMISSION_WAIT_S: Final[float] = 24 * 60 * 60.0
_CONTROL_METHODS: Final[frozenset[str]] = frozenset(
    {"ping", "tools/list", "notifications/initialized", "notifications/cancelled"}
)
_CLIENT_LANE_CONCURRENCY: Final[int] = 64
_CLIENT_ADMISSION_WAIT_S: Final[float] = 5.0
_SESSION_IDLE_S: Final[float] = 3600.0


class McpBackend(Protocol):
    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


BackendFactory = Callable[[Path], McpBackend]


class BackendUnavailable(RuntimeError):
    """The session's root cannot host this backend; the message is shown to the agent."""


def runtime_mode(environment: Mapping[str, str]) -> str:
    """The Runtime install mode ``load_config`` resolves; ``lite`` unless hosted.

    A supervised gateway inherits no shell environment, so the Runtime's own
    settings file (``<state>/runtime/env``) is read the way ``load_config`` reads it,
    including its fallback for older installs that recorded only a URL.
    """

    from ..config import DEFAULT_URL, _runtime_mode, _validated_url, read_settings_file

    home = environment.get("LEMONCROW_HOME", "").strip()
    state_dir = Path(home).expanduser() if home else Path(environment.get("HOME") or Path.home()) / ".lemoncrow"
    merged = {**read_settings_file(state_dir), **read_settings_file(state_dir / "runtime"), **environment}
    try:
        url = _validated_url(merged.get("LEMONCROW_URL", "").strip() or DEFAULT_URL)
        mode = _runtime_mode(merged, url, "runtime")
    except ClientError:
        return "lite"
    return "hosted" if mode == "hosted" else "lite"


def _lite_factory(environment: Mapping[str, str]) -> BackendFactory:
    def create(root: Path) -> McpBackend:
        from ..lite.mcp import LiteMcp

        # LiteMcp also supports workspace folders containing multiple repositories.
        env = {**environment, "LEMONCROW_WORKSPACE_ROOT": str(root)}
        try:
            return LiteMcp(environment=env)
        except RuntimeError as exc:
            raise BackendUnavailable(str(exc)) from exc

    return create


def _thin_factory(environment: Mapping[str, str]) -> BackendFactory:
    from ..dispatcher import _MAX_WORKTREE_SESSIONS
    from ..resolution_cache import cache_budget_bytes

    # Every thin session sizes its response cache as if it owned the process;
    # split one process budget across every session this gateway may hold.
    per_session = cache_budget_bytes(environment.get("LEMONCROW_RESOLUTION_CACHE_BYTES")) // (
        _MAX_SESSIONS * (1 + _MAX_WORKTREE_SESSIONS)
    )

    def create(root: Path) -> McpBackend:
        from ..config import load_config
        from ..mcpserver import McpServer

        env = dict(environment)
        env.setdefault("LEMONCROW_STARTUP_BUDGET_S", "30")
        env.setdefault("LEMONCROW_REQUEST_TIMEOUT_S", "30")
        env["LEMONCROW_RESOLUTION_CACHE_BYTES"] = str(per_session)
        try:
            return McpServer(load_config(env, cwd=root, product="runtime"))
        except ClientError as exc:
            raise BackendUnavailable(str(exc)) from exc

    return create


def backend_factory(environment: Mapping[str, str]) -> BackendFactory:
    return _thin_factory(environment) if runtime_mode(environment) == "hosted" else _lite_factory(environment)


def tool_catalog(mode: str) -> list[dict[str, str]]:
    """The mode's static ``tools/list`` names and descriptions; no backend, no workspace."""

    if mode == "hosted":
        from ..mcpserver import _advertised_tools

        specs: list[Mapping[str, Any]] = list(_advertised_tools())
    else:
        from ..lite.mcp import _surface

        specs = list(_surface())
    return [{"name": str(spec.get("name", "")), "description": str(spec.get("description", ""))} for spec in specs]


@dataclass(slots=True)
class _Session:
    backend: McpBackend
    lock: threading.Lock = field(default_factory=threading.Lock)
    client_slots: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(_CLIENT_LANE_CONCURRENCY)
    )
    active: int = 0
    last_used: float = 0.0

    def busy(self) -> bool:
        background = getattr(self.backend, "background_running", None)
        return self.lock.locked() or self.active > 0 or (callable(background) and bool(background()))


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes = b""
    content_type: str = "application/json"
    headers: Mapping[str, str] = field(default_factory=dict)


def _json(document: Any, status: int = 200, headers: Mapping[str, str] | None = None) -> Response:
    return Response(status, json.dumps(document).encode("utf-8"), "application/json", dict(headers or {}))


_HTML_HEADERS: Final[Mapping[str, str]] = {
    "Cache-Control": "no-store",
    # The consent page must never render inside another site's frame.
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
}
_STALE_PAIRING_CODE: Final[str] = (
    "That pairing code expired or was already used. Get a fresh one with "
    "`lc runtime mcp service code`, or restart `lc runtime mcp serve`."
)


def _html(text: str, status: int = 200) -> Response:
    return Response(status, text.encode("utf-8"), "text/html; charset=utf-8", dict(_HTML_HEADERS))


def _oauth_error(error: str, description: str, status: int = 400) -> Response:
    return _json(
        {"error": error, "error_description": description},
        status,
        {"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _refusal(request_id: object, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": message}], "isError": True},
    }


def _busy(request_id: object) -> Response:
    return _json(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32000,
                "message": "LemonCrow remote MCP admission wait expired; retry this request",
                "data": {"retryable": True},
            },
        },
        429,
        {"Retry-After": "1", "Cache-Control": "no-store"},
    )


def _lane_for(body: Mapping[str, Any], *, concurrent_client_tools: bool) -> str:
    method = body.get("method")
    if not isinstance(method, str):
        return "serial"
    if method in _CONTROL_METHODS:
        return "control"
    if method != "tools/call" or not concurrent_client_tools:
        return "serial"
    params = body.get("params")
    name = params.get("name") if isinstance(params, Mapping) else None
    try:
        route = route_for(str(name))
    except ClientError:
        return "serial"
    return "client" if route.site is Site.CLIENT and not route.pushes_blobs else "serial"


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    query: Mapping[str, str]
    headers: Mapping[str, str]  # lower-cased names
    body: bytes
    host: str

    @property
    def public_authority(self) -> str:
        """``host[:port]`` the caller addressed; the tunnel's, when forwarded."""

        forwarded = self.headers.get("x-forwarded-host", "")
        return (forwarded.split(",", 1)[0].strip() if forwarded else self.host).lower()

    @property
    def public_host(self) -> str:
        return self.public_authority.rsplit(":", 1)[0]

    @property
    def public_base_url(self) -> str:
        # Cloudflare terminates TLS, forwarding HTTP to the loopback gateway.
        # Its origin headers are not guaranteed to include X-Forwarded-Proto.
        # Public connector names always advertise HTTPS; only direct loopback
        # (the --no-tunnel development mode) is allowed to advertise HTTP.
        host = self.public_host
        is_loopback = host in {"localhost", "127.0.0.1", "[::1]"} or host.startswith("127.")
        scheme = "http" if is_loopback else "https"
        return f"{scheme}://{self.public_authority}"

    def form(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, value in urllib.parse.parse_qsl(self.body.decode("utf-8", "replace"), keep_blank_values=True):
            out.setdefault(key, value)
        return out


class RemoteGateway:
    """Transport-independent request handling for the remote MCP gateway."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        factory: BackendFactory | None = None,
        single: ConnectorBinding | None = None,
        pairing_code: str | None = None,
        open_access: bool = False,
    ) -> None:
        """``single`` serves one workspace for every Host (quick-tunnel mode) with
        the unscoped OAuth state; ``pairing_code`` overrides the stored code for
        this process; ``open_access`` drops the bearer check on ``/mcp``."""

        self.environment = dict(os.environ if environment is None else environment)
        self.store_root = store_root_from(self.environment)
        self.mode = runtime_mode(self.environment)
        self._factory = factory or backend_factory(self.environment)
        self._single = single
        self._pairing_override = pairing_code
        self._open_access = open_access
        self._lock = threading.Lock()
        self._pairing_lock = threading.Lock()
        self._stores: dict[str, OAuthStore] = {}
        self._sessions: dict[tuple[str, str, str], _Session] = {}
        self._catalog: list[dict[str, str]] | None = None
        self._capacity = threading.BoundedSemaphore(_WORKERS)

    # -- bindings and OAuth state ------------------------------------------ #

    def binding(self, request: Request) -> ConnectorBinding | None:
        if self._single is not None:
            return self._single
        return load_connector_for_hostname(self.store_root, request.public_host)

    def _scope(self, binding: ConnectorBinding) -> str | None:
        return None if self._single is not None else connector_slug(binding.hostname)

    def _store(self, binding: ConnectorBinding) -> OAuthStore:
        with self._lock:
            store = self._stores.get(binding.hostname)
            if store is None:
                scope = self._scope(binding)
                migrate_legacy_state(self.store_root, scope)
                store = OAuthStore(state_path(self.store_root, scope))
                self._stores[binding.hostname] = store
            return store

    def _redeem_pairing_code(self, binding: ConnectorBinding, submitted: str) -> bool | None:
        """True and retire the code on a match; False on a mismatch; None when no live code exists.

        The gateway never mints a code: only the terminal that shows it does.
        """

        with self._pairing_lock:
            if self._pairing_override is not None:
                if not self._pairing_override:
                    return None
                if not (submitted and hmac.compare_digest(submitted, self._pairing_override)):
                    return False
                self._pairing_override = ""  # a one-off --pairing-code is single use too
                return True
            scope = self._scope(binding)
            migrate_legacy_state(self.store_root, scope)
            path = pairing_path(self.store_root, scope)
            live = current_pairing_code(path)
            if live is None:
                return None
            if not (submitted and hmac.compare_digest(submitted, live)):
                return False
            consume_pairing_code(path, live)
            return True

    # -- dispatch ----------------------------------------------------------- #

    def dispatch(self, request: Request) -> Response:
        if request.path == "/healthz" and request.method in {"GET", "HEAD"}:
            return _json({"ok": True})
        binding = self.binding(request)
        if binding is None:
            return Response(404, b"unknown LemonCrow MCP connector hostname", "text/plain; charset=utf-8")
        route = (request.method, request.path)
        if route in {
            ("GET", "/.well-known/oauth-protected-resource"),
            ("GET", "/.well-known/oauth-protected-resource/mcp"),
        }:
            base = request.public_base_url
            return _json(
                {"resource": base, "authorization_servers": [base], "bearer_methods_supported": ["header"]},
                headers={"Cache-Control": "no-store"},
            )
        if request.method == "GET" and request.path in {
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-authorization-server/mcp",
            "/.well-known/openid-configuration",
            "/.well-known/openid-configuration/mcp",
        }:
            return self._authorization_metadata(request)
        if route == ("GET", "/.well-known/mcp.json"):
            return self._mcp_discovery()
        if route == ("POST", "/register"):
            return self._register(binding, request)
        if route == ("GET", "/authorize"):
            return self._authorize_get(binding, request)
        if route == ("POST", "/authorize"):
            return self._authorize_post(binding, request)
        if route == ("POST", "/token"):
            return self._token(binding, request)
        if request.path == MCP_PATH:
            client_id, refusal = self._bearer(binding, request)
            if refusal is not None:
                return refusal
            if request.method == "POST":
                return self._mcp_post(binding, client_id, request)
            return Response(
                405,
                b"remote MCP is stateless; use POST /mcp",
                "text/plain; charset=utf-8",
                {"Allow": "POST", "Cache-Control": "no-store"},
            )
        return Response(404, b"not found", "text/plain; charset=utf-8")

    @staticmethod
    def _authorization_metadata(request: Request) -> Response:
        base = request.public_base_url
        return _json(
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

    def _mcp_discovery(self) -> Response:
        # Unauthenticated: answered from the static catalog, never by opening
        # the connector's workspace or building a backend.
        if self._catalog is None:
            self._catalog = tool_catalog(self.mode)
        return _json(
            {
                "name": "lc",
                "title": "LemonCrow",
                "transport": {"type": "streamable-http", "endpoint": MCP_PATH},
                "capabilities": {"tools": {}},
                "tools": self._catalog,
            }
        )

    def _register(self, binding: ConnectorBinding, request: Request) -> Response:
        try:
            body = json.loads(request.body)
        except (ValueError, RecursionError):
            return _oauth_error("invalid_client_metadata", "request body is not valid JSON")
        if not isinstance(body, dict):
            return _oauth_error("invalid_client_metadata", "request body must be a JSON object")
        redirect_uris = body.get("redirect_uris")
        if (
            not isinstance(redirect_uris, list)
            or not redirect_uris
            or not all(isinstance(uri, str) for uri in redirect_uris)
        ):
            return _oauth_error(
                "invalid_redirect_uri", "redirect_uris is required and must be a non-empty list of strings"
            )
        for uri in redirect_uris:
            if not is_allowed_redirect_uri(uri):
                return _oauth_error("invalid_redirect_uri", f"redirect_uri must be https (or http loopback): {uri}")
        grant_types = body.get("grant_types")
        response_types = body.get("response_types")
        client_name = body.get("client_name")
        record = self._store(binding).register_client(
            redirect_uris=list(redirect_uris),
            client_name=client_name if isinstance(client_name, str) else None,
            grant_types=list(grant_types) if isinstance(grant_types, list) else ["authorization_code", "refresh_token"],
            response_types=list(response_types) if isinstance(response_types, list) else ["code"],
        )
        return _json(record, 201)

    def _checked_client(
        self, binding: ConnectorBinding, fields: Mapping[str, str]
    ) -> tuple[OAuthStore, str | None, Response | None]:
        store = self._store(binding)
        client = store.get_client(fields.get("client_id", ""))
        if client is None:
            return store, None, _html(error_page("Unknown client_id."), 400)
        if fields.get("redirect_uri", "") not in client["redirect_uris"]:
            return store, None, _html(error_page("redirect_uri does not match a registered value."), 400)
        name = client.get("client_name")
        return store, name if isinstance(name, str) else None, None

    def _authorize_get(self, binding: ConnectorBinding, request: Request) -> Response:
        query = request.query
        _store, client_name, refusal = self._checked_client(binding, query)
        if refusal is not None:
            return refusal
        if query.get("response_type", "") != "code":
            return _html(error_page("response_type must be 'code'."), 400)
        if query.get("code_challenge_method", "") != "S256" or not query.get("code_challenge", ""):
            return _html(error_page("PKCE with code_challenge_method=S256 is required."), 400)
        params = {key: query.get(key, "") for key in AUTHORIZE_FIELDS}
        return _html(render_form(params, error=None, client_name=client_name))

    def _authorize_post(self, binding: ConnectorBinding, request: Request) -> Response:
        form = request.form()
        store, client_name, refusal = self._checked_client(binding, form)
        if refusal is not None:
            return refusal
        params = {key: form.get(key, "") for key in AUTHORIZE_FIELDS}
        client_id = params["client_id"]
        remaining = store.pairing_lockout_remaining(client_id)
        if remaining > 0:
            error = f"Too many attempts. Try again in {int(remaining) + 1}s."
            return _html(render_form(params, error=error, client_name=client_name), 429)
        redeemed = self._redeem_pairing_code(binding, form.get("pairing_code", ""))
        if not redeemed:
            store.record_pairing_failure(client_id)
            error = "Incorrect pairing code." if redeemed is False else _STALE_PAIRING_CODE
            return _html(render_form(params, error=error, client_name=client_name))
        store.reset_pairing_failures(client_id)
        code = store.create_auth_code(
            client_id=params["client_id"],
            redirect_uri=params["redirect_uri"],
            code_challenge=params["code_challenge"],
            resource=params["resource"],
        )
        location = append_query(params["redirect_uri"], {"code": code, "state": params["state"]})
        return Response(302, b"", "text/plain", {"Location": location, "Cache-Control": "no-store"})

    def _token(self, binding: ConnectorBinding, request: Request) -> Response:
        store = self._store(binding)
        form = request.form()
        grant_type = form.get("grant_type", "")
        if grant_type == "authorization_code":
            code = form.get("code", "")
            verifier = form.get("code_verifier", "")
            if not code or not verifier:
                return _oauth_error("invalid_request", "code and code_verifier are required")
            record = store.consume_auth_code(code)
            if record is None:
                return _oauth_error("invalid_grant", "authorization code is invalid or expired")
            if record["client_id"] != form.get("client_id", ""):
                return _oauth_error("invalid_grant", "client_id does not match the authorization code")
            if record["redirect_uri"] != form.get("redirect_uri", ""):
                return _oauth_error("invalid_grant", "redirect_uri does not match the authorization code")
            if not hmac.compare_digest(pkce_s256(verifier), str(record["code_challenge"])):
                return _oauth_error("invalid_grant", "PKCE verification failed")
            return self._token_success(*store.issue_tokens(str(record["client_id"])))
        if grant_type == "refresh_token":
            refresh = form.get("refresh_token", "")
            if not refresh:
                return _oauth_error("invalid_request", "refresh_token is required")
            rotated = store.rotate_refresh_token(refresh)
            if rotated is None:
                return _oauth_error("invalid_grant", "refresh_token is invalid or already used")
            return self._token_success(*rotated)
        return _oauth_error("unsupported_grant_type", f"unsupported grant_type: {grant_type!r}")

    @staticmethod
    def _token_success(access: str, refresh: str) -> Response:
        return _json(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL_SECONDS,
                "refresh_token": refresh,
                "scope": "",
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    def _bearer(self, binding: ConnectorBinding, request: Request) -> tuple[str, Response | None]:
        if self._open_access:
            return "anonymous", None
        scheme, _, raw = request.headers.get("authorization", "").partition(" ")
        token = raw.strip()
        client_id = self._store(binding).access_token_client_id(token) if scheme.lower() == "bearer" and token else None
        if client_id is not None:
            return client_id, None
        return "", _json(
            {"error": "unauthorized", "error_description": "missing or invalid access token"},
            401,
            {
                "Cache-Control": "no-store",
                "WWW-Authenticate": (
                    f'Bearer resource_metadata="{request.public_base_url}/.well-known/oauth-protected-resource"'
                ),
            },
        )

    # -- MCP ---------------------------------------------------------------- #

    def _route_project(
        self, binding: ConnectorBinding, body: dict[str, Any]
    ) -> tuple[dict[str, Any], Path, dict[str, Any] | None]:
        """Resolve ``project_id`` to a directory inside this connector's workspace.

        The connector's bound workspace is the allowlist. A reference that is
        unknown, or resolves outside it, is a tool error -- never a silent fall
        back to the default tree, which would run an ``edit`` in the wrong
        repository.
        """

        home = Path(binding.workspace).resolve()
        params = body.get("params")
        if body.get("method") != "tools/call" or not isinstance(params, dict):
            return body, home, None
        arguments = params.get("arguments")
        if not isinstance(arguments, dict) or "project_id" not in arguments:
            return body, home, None
        reference = arguments["project_id"]
        routed = {**body, "params": {**params, "arguments": {k: v for k, v in arguments.items() if k != "project_id"}}}
        if reference is None or (isinstance(reference, str) and not reference.strip()):
            return routed, home, None
        if not isinstance(reference, str):
            return body, home, _refusal(body.get("id"), "project_id must be a string")
        value = reference.strip()
        resolved = resolve_registered_project(self.store_root, value)
        if resolved is None and not value.startswith("proj_"):
            candidate = Path(value).expanduser()
            candidate = (candidate if candidate.is_absolute() else home / candidate).resolve()
            resolved = candidate if candidate.is_dir() else None
        if resolved is None:
            return body, home, _refusal(body.get("id"), f"unknown project: {value}")
        if not resolved.is_relative_to(home):
            return (
                body,
                home,
                _refusal(body.get("id"), f"project {value} is outside this connector's workspace ({home})"),
            )
        return routed, resolved, None

    def _session(self, binding: ConnectorBinding, client_id: str, root: Path) -> _Session | None:
        """Lease the session for one request; :meth:`_release` returns it.

        A leased session counts as busy, so eviction for another client can
        never close a backend a request has been handed but not yet run.
        """

        key = (binding.hostname, client_id, str(root))
        now = time.monotonic()
        with self._lock:
            session = self._sessions.get(key)
            if session is not None:
                session.active += 1
                session.last_used = now
                return session
            stale = [
                held_key
                for held_key, held in self._sessions.items()
                if not held.busy() and now - held.last_used > _SESSION_IDLE_S
            ]
            while len(self._sessions) - len(stale) >= _MAX_SESSIONS:
                candidates = [
                    (held.last_used, held_key)
                    for held_key, held in self._sessions.items()
                    if held_key not in stale and not held.busy()
                ]
                if not candidates:
                    return None
                stale.append(min(candidates)[1])
            evicted = [self._sessions.pop(held_key) for held_key in stale if held_key in self._sessions]
        for held in evicted:
            held.backend.close()
        backend = self._factory(root)
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                created = backend
                session = existing
            else:
                created = None
                session = _Session(backend=backend, last_used=now)
                self._sessions[key] = session
            session.active += 1
        if created is not None:
            created.close()
        return session

    def _release(self, session: _Session) -> None:
        with self._lock:
            session.active -= 1
            session.last_used = time.monotonic()

    def _run_client_lane(self, session: _Session, body: Mapping[str, Any]) -> dict[str, Any] | Response | None:
        if not session.client_slots.acquire(timeout=_CLIENT_ADMISSION_WAIT_S):
            return _busy(body.get("id"))
        if not self._capacity.acquire(timeout=_CLIENT_ADMISSION_WAIT_S):
            session.client_slots.release()
            return _busy(body.get("id"))
        try:
            return session.backend.handle(body)
        finally:
            self._capacity.release()
            session.client_slots.release()

    def _run_serial_lane(self, session: _Session, body: Mapping[str, Any]) -> dict[str, Any] | Response | None:
        # lc-debt: serial per OAuth client; port a lane admission model if
        # stateful calls ever need to run in parallel for one client.
        if not session.lock.acquire(timeout=_SERIAL_ADMISSION_WAIT_S):
            return _busy(body.get("id"))
        try:
            if not self._capacity.acquire(timeout=_SERIAL_ADMISSION_WAIT_S):
                return _busy(body.get("id"))
            try:
                return session.backend.handle(body)
            finally:
                self._capacity.release()
        finally:
            session.last_used = time.monotonic()
            session.lock.release()

    def _mcp_post(self, binding: ConnectorBinding, client_id: str, request: Request) -> Response:
        if len(request.body) > _MAX_BODY:
            return Response(413, b"request body too large", "text/plain; charset=utf-8")
        try:
            body = json.loads(request.body)
        except (ValueError, RecursionError):
            return _json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        if not isinstance(body, dict):
            return _json({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
        body, root, refusal = self._route_project(binding, body)
        if refusal is not None:
            return _json(refusal)
        try:
            session = self._session(binding, client_id, root)
        except BackendUnavailable as exc:
            if body.get("id") is None:
                return Response(202)
            if body.get("method") == "tools/call":
                return _json(_refusal(body.get("id"), str(exc)))
            return _json({"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32002, "message": str(exc)}})
        if session is None:
            return _busy(body.get("id"))
        # Runtime Lite routes each call through mutable worktree state, so its
        # sessions serialize every tool call. lc-debt: a long Lite ``bash``
        # blocks that client's other calls; split stateless Lite tools into a
        # concurrent lane once LiteMcp's routing is made thread-safe.
        lane = _lane_for(body, concurrent_client_tools=self.mode == "hosted")
        result: dict[str, Any] | Response | None
        try:
            if lane == "control":
                result = session.backend.handle(body)
            elif lane == "client":
                result = self._run_client_lane(session, body)
            else:
                result = self._run_serial_lane(session, body)
        finally:
            self._release(session)
        if isinstance(result, Response):
            return result
        if result is None:
            return Response(202)
        if "text/event-stream" in request.headers.get("accept", "").lower():
            payload = f"data: {json.dumps(result, ensure_ascii=True)}\n\n".encode()
            return Response(200, payload, "text/event-stream")
        return _json(result)

    def close(self) -> None:
        with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.backend.close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LemonCrowRemoteMCP"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def _read_body(self) -> bytes | None:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks: list[bytes] = []
            total = 0
            while True:
                size_line = self.rfile.readline(1024)
                size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline(1024)
                    return b"".join(chunks)
                total += size
                if total > _MAX_BODY:
                    return None
                chunks.append(self.rfile.read(size))
                self.rfile.readline(1024)
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            return None
        return self.rfile.read(length) if length > 0 else b""

    def _respond(self, response: Response) -> None:
        self.send_response(response.status)
        if response.status != 202 or response.body:
            self.send_header("Content-Type", response.content_type)
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    def _handle(self) -> None:
        gateway: RemoteGateway = self.server.gateway  # type: ignore[attr-defined]
        try:
            body = self._read_body()
        except ValueError:
            self._respond(Response(400, b"malformed request body", "text/plain; charset=utf-8"))
            return
        if body is None:
            self.close_connection = True
            self._respond(Response(413, b"request body too large", "text/plain; charset=utf-8"))
            return
        parsed = urllib.parse.urlsplit(self.path)
        query: dict[str, str] = {}
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            query.setdefault(key, value)
        request = Request(
            method=self.command,
            path=parsed.path,
            query=query,
            headers={name.lower(): value for name, value in self.headers.items()},
            body=body,
            host=self.headers.get("Host", ""),
        )
        try:
            response = gateway.dispatch(request)
        except Exception as exc:
            response = _json({"error": "internal_error", "error_description": type(exc).__name__}, 500)
        self._respond(response)

    do_GET = _handle
    do_POST = _handle
    do_HEAD = _handle
    do_DELETE = _handle


class GatewayServer:
    """The gateway bound to a loopback port; a context manager in tests."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        factory: BackendFactory | None = None,
        single: ConnectorBinding | None = None,
        pairing_code: str | None = None,
        open_access: bool = False,
    ) -> None:
        self.gateway = RemoteGateway(
            environment=environment,
            factory=factory,
            single=single,
            pairing_code=pairing_code,
            open_access=open_access,
        )
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self.host = host
        self._server.daemon_threads = True
        self._server.gateway = self.gateway  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> GatewayServer:
        self._thread = threading.Thread(target=self._server.serve_forever, name="lc-remote-mcp", daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        try:
            self._server.serve_forever()
        finally:
            self.close()

    def shutdown(self) -> None:
        self._server.shutdown()

    def close(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5.0)
            self._thread = None
        self._server.server_close()
        self.gateway.close()

    def __enter__(self) -> GatewayServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = [
    "MCP_PATH",
    "BackendUnavailable",
    "GatewayServer",
    "RemoteGateway",
    "Request",
    "Response",
    "backend_factory",
    "runtime_mode",
]
