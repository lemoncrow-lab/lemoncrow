"""Single-user OAuth 2.1 shim so ChatGPT can reach LemonCrow's MCP transport.

ChatGPT's *custom MCP connector* (Settings → Connectors → Developer Mode) only
speaks two authentication dialects: **no-auth** or **OAuth 2.1 with dynamic
client registration (DCR)**. LemonCrow's streamable-HTTP transport
(``mcp_http.py``) exposes shell-grade tools, so no-auth over a public tunnel is
out of the question. This module supplies the smallest possible OAuth 2.1
authorization server that satisfies ChatGPT while gating ``/mcp`` behind a
single human secret — a *pairing code* printed at startup.

Design constraints that shape everything below:

  * **One human, one secret.** There is exactly one operator. We do not model
    users, consent screens, or scopes — the pairing code is the whole identity
    story. The OAuth machinery (PKCE, DCR, refresh rotation) exists only because
    ChatGPT demands it, not because we have multiple principals to separate.

  * **Behind a tunnel.** The process binds to loopback and is reached through
    cloudflared/ngrok, so the URL ChatGPT sees is the *tunnel's* public URL, not
    ``localhost``. Every advertised URL (issuer, endpoints, resource id, the
    ``WWW-Authenticate`` discovery hint) must therefore be derived per-request
    from the forwarding headers — see ``_public_base_url``.

  * **Secrets never rest in plaintext.** Access/refresh tokens are persisted
    only as SHA-256 hex digests; the plaintext exists just long enough to hand
    back in the token response. A leaked state file cannot be replayed.

  * **Survive restarts, but stay disposable.** Registered clients and token
    hashes live in a 0600 JSON file so ChatGPT keeps working across a reconnect;
    short-lived authorization codes stay in memory only. ``--reset`` (or a
    corrupt file) starts fresh, revoking everything.

Wiring: ``create_protected_mcp_app`` builds a FastAPI app that mounts the OAuth
endpoints (all public) plus the MCP transport gated by a bearer dependency via
``register_mcp_http(auth_dependency=...)`` — we do not modify ``mcp_http.py``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp_http import register_mcp_http

logger = logging.getLogger(__name__)

# ── Tunables ────────────────────────────────────────────────────────────────
# 30 days: ChatGPT holds the connector for a long time; a short access-token TTL
# would force constant refreshes. The refresh token rotates on every use, so a
# stolen access token still expires on its own.
ACCESS_TOKEN_TTL_SECONDS = 2_592_000
# Authorization codes are one-shot and exchanged within seconds of the browser
# redirect; 120s is generous slack for a slow tunnel round-trip.
AUTH_CODE_TTL_SECONDS = 120.0
# The pairing code is the only human secret and lives behind a public URL, so a
# brute-force gate is mandatory. After this many wrong guesses we stop answering
# for the lockout window regardless of how fast the attacker retries.
MAX_PAIRING_FAILURES = 5
PAIRING_LOCKOUT_SECONDS = 60.0

_DEFAULT_STATE_FILENAME = "oauth.json"


# ── URL / request helpers ─────────────────────────────────────────────────────
def _first(value: str | None) -> str:
    """Take the first entry of a possibly comma-joined forwarded header."""
    return (value or "").split(",")[0].strip()


def _public_base_url(request: Request) -> str:
    """Reconstruct the *public* origin the client used to reach us.

    Uvicorn only sees the loopback bind, so ``request.url`` reports ``localhost``.
    The tunnel (cloudflared/ngrok) forwards the real scheme/host via the standard
    ``X-Forwarded-*`` headers; we trust those first and fall back to what the ASGI
    layer saw. Every issuer/endpoint/resource URL we advertise flows through here
    so ChatGPT is never handed a ``localhost`` it cannot reach.
    """
    scheme = _first(request.headers.get("x-forwarded-proto")) or request.url.scheme
    host = _first(request.headers.get("x-forwarded-host")) or _first(request.headers.get("host")) or request.url.netloc
    return f"{scheme}://{host}"


def _form_str(form: FormData, key: str) -> str:
    """Read a single form field as ``str`` (uploads/absent → empty string)."""
    value = form.get(key)
    return value if isinstance(value, str) else ""


def _is_allowed_redirect_uri(uri: str) -> bool:
    """Public clients must use https; only loopback http is tolerated (dev)."""
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme == "https":
        return True
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"):
        return True
    return False


def _append_query(url: str, params: dict[str, str]) -> str:
    """Append query params to a redirect URI, preserving any it already carries."""
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(params.items())
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def _pkce_s256(code_verifier: str) -> str:
    """BASE64URL-NoPad(SHA256(verifier)) — the RFC 7636 S256 challenge."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── State persistence ─────────────────────────────────────────────────────────
def default_state_path() -> Path:
    """Where OAuth state lives by default: ``<store_root>/chatgpt/oauth.json``.

    ``<store_root>`` is ``default_store_root()`` (``~/.lemoncrow``, or
    ``$LEMONCROW_ROOT`` when set) — the same root every other LemonCrow
    on-disk state lives under (``sessions/``, ``mitm/``, ``settings.json``,
    …), with ``chatgpt/`` as this feature's peer subdirectory. This used to be
    ``$XDG_STATE_HOME/lemoncrow/chatgpt_oauth.json``; that XDG-rooted file is
    NOT migrated (no code moves it), so anyone who paired a ChatGPT connector
    before this change will need to re-pair once — a one-time, accepted
    wrinkle in exchange for keeping all LemonCrow state under one root instead
    of split across XDG directories. Parent dirs are created lazily on first
    write.
    """
    return default_store_root() / "chatgpt" / _DEFAULT_STATE_FILENAME


def reset_state(state_path: Path) -> bool:
    """Delete the persisted state file (revokes all clients + tokens).

    Returns True if a file was removed. Safe to call when nothing exists.
    """
    try:
        state_path.unlink()
        return True
    except FileNotFoundError:
        return False


class _OAuthStore:
    """Thread-safe store for OAuth state, with a 0600 JSON persistence layer.

    Persisted (survives restart): registered clients, access-token hashes,
    refresh-token hashes. In-memory only (deliberately volatile): authorization
    codes (single-use, seconds-lived) and the pairing-code brute-force counter.
    All mutations that touch persisted state flush the whole file atomically.
    """

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._lock = threading.RLock()
        # client_id -> registration record (includes redirect_uris for matching)
        self._clients: dict[str, dict[str, Any]] = {}
        # sha256(access_token) -> {"client_id": str, "expires_at": float epoch}
        self._access_tokens: dict[str, dict[str, Any]] = {}
        # sha256(refresh_token) -> {"client_id": str}
        self._refresh_tokens: dict[str, dict[str, Any]] = {}
        # code -> {"client_id", "redirect_uri", "code_challenge", "expires", "resource"}
        self._auth_codes: dict[str, dict[str, Any]] = {}
        self._pairing_failures = 0
        self._pairing_lockout_until = 0.0
        self._load()

    # -- persistence ----------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("could not read OAuth state %s (%s); starting fresh", self.state_path, exc)
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("corrupt OAuth state %s (%s); starting fresh", self.state_path, exc)
            return
        if not isinstance(data, dict):
            logger.warning("unexpected OAuth state shape in %s; starting fresh", self.state_path)
            return
        clients = data.get("clients")
        access = data.get("access_tokens")
        refresh = data.get("refresh_tokens")
        if isinstance(clients, dict):
            self._clients = clients
        if isinstance(access, dict):
            self._access_tokens = access
        if isinstance(refresh, dict):
            self._refresh_tokens = refresh

    def _save(self) -> None:
        """Atomically write the persisted state with 0600 permissions.

        tempfile+rename so a crash mid-write never leaves a truncated file that
        would lock the operator out on the next start. ``mkstemp`` already opens
        at 0600; we chmod again defensively and ``os.replace`` carries those
        perms onto the destination.
        """
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "clients": self._clients,
            "access_tokens": self._access_tokens,
            "refresh_tokens": self._refresh_tokens,
        }
        fd, tmp = tempfile.mkstemp(dir=str(self.state_path.parent), prefix=".chatgpt_oauth.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # -- clients (DCR) --------------------------------------------------------
    def register_client(
        self,
        *,
        redirect_uris: list[str],
        client_name: str | None,
        grant_types: list[str],
        response_types: list[str],
        user_defined: bool = False,
    ) -> dict[str, Any]:
        client_id = secrets.token_urlsafe(24)
        issued_at = int(time.time())
        record: dict[str, Any] = {
            "client_id": client_id,
            "client_id_issued_at": issued_at,
            "redirect_uris": redirect_uris,
            # Public client (PKCE-only); ChatGPT holds no secret it could leak.
            "token_endpoint_auth_method": "none",
            "grant_types": grant_types,
            "response_types": response_types,
        }
        if client_name is not None:
            record["client_name"] = client_name
        if user_defined:
            # Operator-minted client (``lc chatgpt client``) as opposed to DCR:
            # the tag lets later invocations find and reuse it (stable ID).
            record["user_defined"] = True
        with self._lock:
            self._clients[client_id] = record
            self._save()
        return record

    def ensure_user_client(self, redirect_uris: list[str]) -> dict[str, Any]:
        """Get-or-create the operator's user-defined client (idempotent).

        Matches on the *set* of redirect URIs so ordering does not mint
        duplicates; a different URI set is a different client on purpose.
        """
        wanted = set(redirect_uris)
        with self._lock:
            for record in self._clients.values():
                if record.get("user_defined") and set(record.get("redirect_uris", [])) == wanted:
                    return record
        return self.register_client(
            redirect_uris=redirect_uris,
            client_name="ChatGPT (user-defined)",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            user_defined=True,
        )

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._clients.get(client_id)

    # -- authorization codes (in-memory, single-use) --------------------------
    def create_auth_code(self, *, client_id: str, redirect_uri: str, code_challenge: str, resource: str) -> str:
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._auth_codes[code] = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "resource": resource,
                "expires": time.monotonic() + AUTH_CODE_TTL_SECONDS,
            }
        return code

    def consume_auth_code(self, code: str) -> dict[str, Any] | None:
        """Pop a code (single-use) and reject it if expired."""
        with self._lock:
            record = self._auth_codes.pop(code, None)
        if record is None:
            return None
        if time.monotonic() > record["expires"]:
            return None
        return record

    # -- tokens ---------------------------------------------------------------
    def issue_tokens(self, client_id: str) -> tuple[str, str]:
        """Mint a fresh access+refresh pair, persisting only their hashes."""
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        with self._lock:
            self._access_tokens[_sha256_hex(access_token)] = {
                "client_id": client_id,
                "expires_at": time.time() + ACCESS_TOKEN_TTL_SECONDS,
            }
            self._refresh_tokens[_sha256_hex(refresh_token)] = {"client_id": client_id}
            self._save()
        return access_token, refresh_token

    def verify_access_token(self, token: str) -> bool:
        """Constant-work lookup by hash; drops the entry if past its TTL."""
        digest = _sha256_hex(token)
        with self._lock:
            record = self._access_tokens.get(digest)
            if record is None:
                return False
            if time.time() > record["expires_at"]:
                del self._access_tokens[digest]
                self._save()
                return False
            return True

    def rotate_refresh_token(self, refresh_token: str) -> tuple[str, str] | None:
        """Revoke the presented refresh token and issue a new access+refresh.

        Refresh-token rotation: the old token is single-use, so a replayed
        refresh (e.g. a leaked one used after ChatGPT already rotated) fails.
        """
        digest = _sha256_hex(refresh_token)
        with self._lock:
            record = self._refresh_tokens.pop(digest, None)
            if record is None:
                return None
            client_id = str(record["client_id"])
        return self.issue_tokens(client_id)

    # -- pairing-code brute-force gate ---------------------------------------
    def pairing_lockout_remaining(self) -> float:
        with self._lock:
            remaining = self._pairing_lockout_until - time.monotonic()
            return remaining if remaining > 0 else 0.0

    def record_pairing_failure(self) -> None:
        with self._lock:
            self._pairing_failures += 1
            if self._pairing_failures >= MAX_PAIRING_FAILURES:
                self._pairing_lockout_until = time.monotonic() + PAIRING_LOCKOUT_SECONDS

    def reset_pairing_failures(self) -> None:
        with self._lock:
            self._pairing_failures = 0
            self._pairing_lockout_until = 0.0


def ensure_user_client(state_path: Path, redirect_uris: list[str]) -> dict[str, Any]:
    """Get-or-create a user-defined OAuth client in the persisted state store.

    For ChatGPT's "Enter a client ID" connector field (``lc chatgpt client``):
    opens the same state file ``serve`` uses so the minted client works against
    the running server and survives restarts.
    """
    return _OAuthStore(state_path).ensure_user_client(redirect_uris)


# ── HTML rendering ─────────────────────────────────────────────────────────────
# The authorization page is intentionally self-contained (no external CSS/JS):
# it renders inside ChatGPT's OAuth popup which blocks third-party assets, and it
# is the one surface a human actually sees.
_PAGE_STYLE = (
    "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
    "background:#0f1115;color:#e6e6e6;display:flex;min-height:100vh;margin:0;"
    "align-items:center;justify-content:center}"
    ".card{background:#1b1e26;border:1px solid #2a2e38;border-radius:12px;padding:32px;"
    "max-width:380px;width:100%;box-shadow:0 8px 30px rgba(0,0,0,.4)}"
    "h1{font-size:18px;margin:0 0 8px}p{font-size:14px;line-height:1.5;color:#a0a6b0}"
    "input[type=text]{width:100%;box-sizing:border-box;padding:10px 12px;margin:14px 0;"
    "font-size:15px;border-radius:8px;border:1px solid #3a3f4b;background:#11141a;color:#fff}"
    "button{width:100%;padding:11px;font-size:15px;font-weight:600;border:0;border-radius:8px;"
    "background:#f2c94c;color:#1b1e26;cursor:pointer}"
    ".error{color:#ff6b6b;font-size:13px;margin-top:4px}"
)

_AUTHORIZE_FIELDS = (
    "client_id",
    "redirect_uri",
    "response_type",
    "state",
    "code_challenge",
    "code_challenge_method",
    "scope",
    "resource",
)


def _error_page(message: str) -> str:
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>LemonCrow</title>"
        f"<style>{_PAGE_STYLE}</style></head><body><div class='card'>"
        f"<h1>Cannot continue</h1><p>{html.escape(message)}</p></div></body></html>"
    )


def _render_form(params: dict[str, str], error: str | None) -> str:
    hidden = "".join(
        f"<input type='hidden' name='{html.escape(key)}' value='{html.escape(params.get(key, ''))}'>"
        for key in _AUTHORIZE_FIELDS
    )
    error_html = f"<p class='error'>{html.escape(error)}</p>" if error else ""
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>Connect ChatGPT to LemonCrow</title>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<style>{_PAGE_STYLE}</style></head><body><div class='card'>"
        f"<h1>Connect ChatGPT to LemonCrow</h1>"
        f"<p>Enter the pairing code shown in your terminal to authorize this connector.</p>"
        f"<form method='post' action='/authorize'>{hidden}"
        f"<input type='text' name='pairing_code' autocomplete='off' autofocus "
        f"placeholder='pairing code' spellcheck='false'>{error_html}"
        f"<button type='submit'>Authorize</button></form></div></body></html>"
    )


# ── OAuth error responses (RFC 6749 / 7591 shapes) ─────────────────────────────
def _token_error(error: str, description: str, *, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _register_error(error: str, description: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=400)


# ── App factory ────────────────────────────────────────────────────────────────
def create_protected_mcp_app(
    *,
    pairing_code: str,
    state_path: Path | None = None,
    path: str = "/mcp",
) -> FastAPI:
    """Build a FastAPI app: OAuth 2.1 shim + bearer-gated MCP transport.

    The OAuth endpoints (well-known metadata, ``/register``, ``/authorize``,
    ``/token``) stay public — they *are* the login flow. Only ``/mcp`` is gated,
    via a bearer dependency handed to ``register_mcp_http`` so the transport code
    (``mcp_http.py``) is reused untouched.
    """
    store = _OAuthStore(state_path if state_path is not None else default_state_path())

    app = FastAPI(
        title="LemonCrow MCP (ChatGPT OAuth)",
        version=mcp_server.SERVER_VERSION,
        description="OAuth 2.1-protected streamable-HTTP MCP transport for ChatGPT connectors.",
    )

    # -- RFC 9728: protected-resource metadata --------------------------------
    def _protected_resource_doc(request: Request) -> dict[str, Any]:
        base = _public_base_url(request)
        return {
            "resource": base,
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
        }

    @app.get("/.well-known/oauth-protected-resource")
    async def protected_resource(request: Request) -> JSONResponse:
        return JSONResponse(_protected_resource_doc(request))

    # Some clients append the resource path to the well-known URL; serve it too.
    @app.get(f"/.well-known/oauth-protected-resource{path}")
    async def protected_resource_with_path(request: Request) -> JSONResponse:
        return JSONResponse(_protected_resource_doc(request))

    # -- RFC 8414: authorization-server metadata ------------------------------
    def _authorization_server_doc(request: Request) -> dict[str, Any]:
        base = _public_base_url(request)
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [],
        }

    async def authorization_server_metadata(request: Request) -> JSONResponse:
        return JSONResponse(_authorization_server_doc(request))

    # Clients probe several spellings of this document: the RFC 8414 canonical
    # path, the RFC 8414 path-component form (issuer with a path appended, e.g.
    # /.well-known/oauth-authorization-server/mcp), and the OIDC discovery
    # aliases. Observed live: after fetching both OAuth metadata documents,
    # ChatGPT still probes GET /.well-known/openid-configuration — a 404 there
    # stalls the connector, so the same document is served at every alias.
    for _metadata_route in (
        "/.well-known/oauth-authorization-server",
        f"/.well-known/oauth-authorization-server{path}",
        "/.well-known/openid-configuration",
        f"/.well-known/openid-configuration{path}",
    ):
        app.get(_metadata_route)(authorization_server_metadata)

    # -- RFC 7591: dynamic client registration --------------------------------
    @app.post("/register")
    async def register_dcr(request: Request) -> Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return _register_error("invalid_client_metadata", "request body is not valid JSON")
        if not isinstance(body, dict):
            return _register_error("invalid_client_metadata", "request body must be a JSON object")
        redirect_uris = body.get("redirect_uris")
        if (
            not isinstance(redirect_uris, list)
            or not redirect_uris
            or not all(isinstance(uri, str) for uri in redirect_uris)
        ):
            return _register_error(
                "invalid_redirect_uri", "redirect_uris is required and must be a non-empty list of strings"
            )
        for uri in redirect_uris:
            if not _is_allowed_redirect_uri(uri):
                return _register_error("invalid_redirect_uri", f"redirect_uri must be https (or http loopback): {uri}")
        client_name = body.get("client_name")
        grant_types = body.get("grant_types") or ["authorization_code", "refresh_token"]
        response_types = body.get("response_types") or ["code"]
        record = store.register_client(
            redirect_uris=list(redirect_uris),
            client_name=client_name if isinstance(client_name, str) else None,
            grant_types=list(grant_types) if isinstance(grant_types, list) else ["authorization_code", "refresh_token"],
            response_types=list(response_types) if isinstance(response_types, list) else ["code"],
        )
        return JSONResponse(record, status_code=201)

    # -- Authorization endpoint (human-facing) --------------------------------
    @app.get("/authorize")
    async def authorize_get(request: Request) -> Response:
        q = request.query_params
        client_id = q.get("client_id", "")
        redirect_uri = q.get("redirect_uri", "")
        client = store.get_client(client_id)
        # redirect_uri must be validated BEFORE any redirect could happen, and a
        # bad client_id/redirect_uri must render an error page, never bounce to a
        # possibly-attacker-controlled URI (OAuth 2.1 §4.1.2.1 / open-redirect).
        if client is None:
            return HTMLResponse(_error_page("Unknown client_id."), status_code=400)
        if redirect_uri not in client["redirect_uris"]:
            return HTMLResponse(_error_page("redirect_uri does not match a registered value."), status_code=400)
        if q.get("response_type", "") != "code":
            return HTMLResponse(_error_page("response_type must be 'code'."), status_code=400)
        if q.get("code_challenge_method", "") != "S256" or not q.get("code_challenge", ""):
            # Reject plain PKCE (or none): S256 is the only method we advertise.
            return HTMLResponse(_error_page("PKCE with code_challenge_method=S256 is required."), status_code=400)
        params = {key: q.get(key, "") for key in _AUTHORIZE_FIELDS}
        return HTMLResponse(_render_form(params, error=None))

    @app.post("/authorize")
    async def authorize_post(request: Request) -> Response:
        form = await request.form()
        client_id = _form_str(form, "client_id")
        redirect_uri = _form_str(form, "redirect_uri")
        client = store.get_client(client_id)
        # Re-validate against registration: hidden fields are attacker-editable.
        if client is None:
            return HTMLResponse(_error_page("Unknown client_id."), status_code=400)
        if redirect_uri not in client["redirect_uris"]:
            return HTMLResponse(_error_page("redirect_uri does not match a registered value."), status_code=400)
        params = {key: _form_str(form, key) for key in _AUTHORIZE_FIELDS}

        lockout = store.pairing_lockout_remaining()
        if lockout > 0:
            return HTMLResponse(
                _render_form(params, error=f"Too many attempts. Try again in {int(lockout) + 1}s."),
                status_code=429,
            )
        submitted = _form_str(form, "pairing_code")
        # Constant-time compare so response timing does not leak how many leading
        # characters matched — the only secret gating shell access.
        if not (submitted and hmac.compare_digest(submitted, pairing_code)):
            store.record_pairing_failure()
            return HTMLResponse(_render_form(params, error="Incorrect pairing code."), status_code=200)
        store.reset_pairing_failures()
        code = store.create_auth_code(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=params["code_challenge"],
            resource=params["resource"],
        )
        location = _append_query(redirect_uri, {"code": code, "state": params["state"]})
        return RedirectResponse(location, status_code=302)

    # -- Token endpoint -------------------------------------------------------
    @app.post("/token")
    async def token(request: Request) -> Response:
        form = await request.form()
        grant_type = _form_str(form, "grant_type")
        if grant_type == "authorization_code":
            return _exchange_auth_code(store, form)
        if grant_type == "refresh_token":
            return _exchange_refresh(store, form)
        return _token_error("unsupported_grant_type", f"unsupported grant_type: {grant_type!r}")

    # -- Bearer dependency gating /mcp ----------------------------------------
    def _require_bearer(request: Request) -> None:
        """Gate ``/mcp`` on a valid bearer access token.

        The ``WWW-Authenticate: Bearer resource_metadata=...`` header is *load
        bearing*: it is exactly how ChatGPT discovers there is an OAuth flow to
        run. A 401 without it leaves the connector stuck, so it must be present
        on every rejection and point at the per-request public resource metadata.
        """
        header = request.headers.get("authorization", "")
        scheme, _, raw = header.partition(" ")
        token_value = raw.strip()
        if scheme.lower() == "bearer" and token_value and store.verify_access_token(token_value):
            return
        base = _public_base_url(request)
        raise HTTPException(
            status_code=401,
            detail="missing or invalid access token",
            headers={"WWW-Authenticate": (f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"')},
        )

    # Reuse the untouched MCP transport; only /mcp is gated, discovery stays open.
    register_mcp_http(app, path=path, auth_dependency=_require_bearer)
    return app


def _exchange_auth_code(store: _OAuthStore, form: FormData) -> Response:
    code = _form_str(form, "code")
    client_id = _form_str(form, "client_id")
    redirect_uri = _form_str(form, "redirect_uri")
    code_verifier = _form_str(form, "code_verifier")
    if not code or not code_verifier:
        return _token_error("invalid_request", "code and code_verifier are required")
    # Consume first: a failed exchange still burns the one-shot code so it cannot
    # be retried by an attacker who guessed a verifier.
    record = store.consume_auth_code(code)
    if record is None:
        return _token_error("invalid_grant", "authorization code is invalid or expired")
    if record["client_id"] != client_id:
        return _token_error("invalid_grant", "client_id does not match the authorization code")
    if record["redirect_uri"] != redirect_uri:
        return _token_error("invalid_grant", "redirect_uri does not match the authorization code")
    if not hmac.compare_digest(_pkce_s256(code_verifier), str(record["code_challenge"])):
        return _token_error("invalid_grant", "PKCE verification failed")
    access_token, refresh_token = store.issue_tokens(client_id)
    return _token_success(access_token, refresh_token)


def _exchange_refresh(store: _OAuthStore, form: FormData) -> Response:
    refresh_token = _form_str(form, "refresh_token")
    if not refresh_token:
        return _token_error("invalid_request", "refresh_token is required")
    rotated = store.rotate_refresh_token(refresh_token)
    if rotated is None:
        return _token_error("invalid_grant", "refresh_token is invalid or already used")
    access_token, new_refresh_token = rotated
    return _token_success(access_token, new_refresh_token)


def _token_success(access_token: str, refresh_token: str) -> JSONResponse:
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": refresh_token,
            "scope": "",
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
