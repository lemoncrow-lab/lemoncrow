"""Scoped browser authentication for the loopback Review surface.

The machine bearer remains the authority for CLI/MCP/server APIs. A browser
gets a separate Review-only capability after an authenticated CLI arms a short
pairing window. The capability is origin-bound (including the listener port),
so it belongs in browser origin storage rather than a localhost cookie.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
from collections.abc import Callable
from typing import Final

from aiohttp import web

from .errors import AgentAction, ErrorCode, ServerError

LOCAL_BROWSER_PAIR_PATH: Final[str] = "/v1/auth/local-browser/pair"
LOCAL_BROWSER_CLAIM_PATH: Final[str] = "/v1/auth/local-browser/claim"
LOCAL_BROWSER_SESSION_HEADER: Final[str] = "X-LemonCrow-Browser-Session"
LOCAL_BROWSER_PAIR_TTL_S: Final[float] = 20.0
LOCAL_BROWSER_SESSION_TTL_S: Final[float] = 30 * 24 * 60 * 60.0

_FORWARDING_HEADERS: Final[tuple[str, ...]] = (
    "Forwarded",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
    "X-Real-IP",
    "CF-Connecting-IP",
    "True-Client-IP",
)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def browser_authorized_path(path: str) -> bool:
    """Whether a paired browser capability may authenticate this request."""

    return path == "/v1/reviews" or path.startswith("/api/")


def local_browser_refusal(request: web.Request, *, require_same_origin_context: bool = False) -> str:
    """Return why a request is not a direct loopback browser trust context.

    Machine-authenticated pairing calls set ``require_same_origin_context=False``
    because a CLI has no browser Origin. Browser claims and mutating Review API
    calls require an explicit same-origin Origin/Referer in addition to loopback.
    """

    transport = request.transport
    if transport is None:
        return "local browser request has no transport"
    peer = transport.get_extra_info("peername")
    if not isinstance(peer, tuple) or not peer:
        return "local browser request has no peer address"
    try:
        if not ipaddress.ip_address(str(peer[0])).is_loopback:
            return "local browser request did not originate on loopback"
    except ValueError:
        return "local browser request has an invalid peer address"

    for name in _FORWARDING_HEADERS:
        if request.headers.get(name, "").strip():
            return f"local browser trust is disabled for proxied requests ({name})"

    sockname = transport.get_extra_info("sockname")
    if not isinstance(sockname, tuple) or len(sockname) < 2:
        return "local browser request has no listener address"
    try:
        port = int(sockname[1])
    except (TypeError, ValueError):
        return "local browser request has an invalid listener port"

    host = request.headers.get("Host", "").strip().lower()
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    if host not in allowed_hosts:
        return "local browser trust requires a literal loopback Host"

    origin = f"{request.scheme}://{host}"
    presented_origin = request.headers.get("Origin", "").strip().lower()
    if presented_origin and presented_origin != origin:
        return "local browser Origin is not this server"
    referer = request.headers.get("Referer", "").strip().lower()
    if referer and not (referer == origin or referer.startswith(origin + "/")):
        return "local browser Referer is not this server"
    if require_same_origin_context and not (presented_origin or referer):
        return "local browser request requires explicit same-origin context"
    fetch_site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
    if fetch_site and fetch_site != "same-origin":
        return "local browser request requires same-origin fetch metadata"
    return ""


class LocalBrowserSessions:
    """Stateless Review-only browser capabilities plus a tiny pairing window."""

    def __init__(self, machine_token: str, *, clock: Callable[[], float]) -> None:
        if len(machine_token) < 16:
            raise ServerError(ErrorCode.NOT_CONFIGURED, "local browser auth requires the machine credential")
        self._key = hmac.new(
            machine_token.encode("utf-8"),
            b"lemoncrow/local-browser-session/v1",
            hashlib.sha256,
        ).digest()
        self._clock = clock
        self._pending: dict[str, float] = {}

    def arm(self, path: str) -> None:
        now = self._clock()
        self._pending = {key: deadline for key, deadline in self._pending.items() if deadline >= now}
        self._pending[path] = now + LOCAL_BROWSER_PAIR_TTL_S

    def claim(self, path: str, host: str) -> tuple[str, int]:
        now = self._clock()
        deadline = self._pending.pop(path, 0.0)
        if deadline < now:
            raise ServerError(
                ErrorCode.UNAUTHENTICATED,
                "local Review browser is not paired; open the Reviews directory or run lc review --open once",
                action=AgentAction.REAUTHENTICATE,
            )
        return self.issue(host)

    def issue(self, host: str) -> tuple[str, int]:
        """Mint an origin-bound browser capability for an already trusted local UI."""

        now = self._clock()
        expires_at = int(now + LOCAL_BROWSER_SESSION_TTL_S)
        nonce = secrets.token_urlsafe(18)
        signature = self._signature(expires_at, nonce, host)
        return f"v1.{expires_at}.{nonce}.{signature}", expires_at

    def verify(self, token: str, host: str) -> bool:
        try:
            version, raw_expiry, nonce, signature = token.split(".", 3)
            expires_at = int(raw_expiry)
        except (TypeError, ValueError):
            return False
        if version != "v1" or expires_at < int(self._clock()) or not nonce or not signature:
            return False
        expected = self._signature(expires_at, nonce, host)
        return hmac.compare_digest(signature.encode("ascii", errors="ignore"), expected.encode("ascii"))

    def _signature(self, expires_at: int, nonce: str, host: str) -> str:
        payload = f"v1\n{expires_at}\n{nonce}\n{host.lower()}".encode()
        return _b64url(hmac.new(self._key, payload, hashlib.sha256).digest())
