"""Authenticated, isolated proxy in front of one interactive preview side.

Origin isolation (the documented choice): every side of every session is
served from **its own origin** -- locally a dedicated loopback listener
(``127.0.0.1:<ephemeral port>``), hosted a dedicated host name under the
deployment's isolated preview domain -- and the Reader embeds it in a
sandboxed iframe (``allow-scripts allow-forms allow-same-origin``; no top
navigation, no popups escaping the sandbox). The preview origin never holds a
LemonCrow credential: the local Reader authenticates with a bearer token kept
in script memory of its own origin, the hosted Reader's session cookie is
host-only on the Reader host, and this proxy forwards upstream only cookies the
preview itself set (namespaced per session side), so preview content can
neither read nor replay a LemonCrow session.

Every request is authorized here, not by the upstream:

* entry is a one-time, short-lived ticket minted by the authenticated Reader
  API for the session's own viewer. It travels in the URL *fragment*
  (``/__lemoncrow/enter#ticket=...``), which browsers never send, so no
  request line or access log ever carries it; the entry page POSTs it back
  and it is exchanged for an HttpOnly capability cookie scoped to this
  origin, then never valid again. Tickets and capabilities are kept only as
  digests in the state every replica shares (``preview_state``); a ticket is
  taken atomically, so it is redeemed once across all replicas;
* each later request needs that capability, and the session gate (state, TTL
  on an injected clock, viewer still authorized for the Review) is checked
  per request -- an expired session is refused with 410 even before the
  sweeper tears it down;
* the upstream is fixed when the side starts and is never derived from the
  request: absolute-form and ``CONNECT`` targets are refused, so the proxy can
  never be steered at internal services or cloud metadata (SSRF);
* request/response headers are allowlisted, upstream redirects may only stay
  on the preview origin (no open redirect), responses carry a CSP whose
  ``connect-src 'self'`` keeps browser-side code on this origin, and response
  size and upstream time are capped.

Nothing here logs the request line, a ticket or a capability.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import parse_qs, quote, urlsplit

from lemoncrow.pro.environment.preview_state import InMemoryPreviewState, PreviewState, side_key

ENTER_PATH: Final[str] = "/__lemoncrow/enter"
TICKET_TTL_SECONDS: Final[float] = 60.0
MAX_OUTSTANDING_TICKETS: Final[int] = 16
#: Capabilities (one per redeemed ticket) kept per side; the oldest is forgotten beyond it.
MAX_CAPABILITIES: Final[int] = 16
#: Upper bound on a capability's life; the session gate and teardown end it sooner.
CAPABILITY_TTL_SECONDS: Final[float] = 1800.0
MAX_REQUEST_BYTES: Final[int] = 1024 * 1024
MAX_RESPONSE_BYTES: Final[int] = 16 * 1024 * 1024
UPSTREAM_TIMEOUT_SECONDS: Final[float] = 20.0
MAX_TARGET_CHARS: Final[int] = 8192
MAX_SET_COOKIES: Final[int] = 20
ALLOWED_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_REQUEST_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "accept",
        "accept-language",
        "cache-control",
        "content-type",
        "if-match",
        "if-modified-since",
        "if-none-match",
        "pragma",
        "range",
        "user-agent",
        "x-requested-with",
    }
)
_RESPONSE_HEADERS: Final[frozenset[str]] = frozenset(
    {"content-type", "content-language", "etag", "last-modified", "vary", "accept-ranges", "content-range", "allow"}
)
_COOKIE_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")
_KEPT_COOKIE_ATTRIBUTES: Final[frozenset[str]] = frozenset({"expires", "max-age", "httponly", "samesite", "secure"})
_LOOPBACK_NAMES: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})
#: What the upstream app sees as its own origin (the sandbox has nothing else).
UPSTREAM_ORIGIN: Final[str] = "http://localhost"


class PreviewUpstreamError(RuntimeError):
    """The sandboxed app did not answer (502)."""


class PreviewUpstreamTimeout(PreviewUpstreamError):
    """The sandboxed app answered too slowly (504)."""


class PreviewResponseTooLarge(PreviewUpstreamError):
    """The sandboxed app's response exceeded the size cap (502)."""


@dataclass(frozen=True, slots=True)
class ProxyRequest:
    method: str
    target: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""

    def header(self, name: str) -> str:
        wanted = name.lower()
        return next((value for key, value in self.headers if key.lower() == wanted), "")


@dataclass(frozen=True, slots=True)
class ProxyResponse:
    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""

    def header(self, name: str) -> str:
        wanted = name.lower()
        return next((value for key, value in self.headers if key.lower() == wanted), "")

    def header_values(self, name: str) -> list[str]:
        wanted = name.lower()
        return [value for key, value in self.headers if key.lower() == wanted]


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Whether the session may serve right now (state, TTL, viewer authorization)."""

    allowed: bool
    status: int = 200
    reason: str = ""


Forward = Callable[..., ProxyResponse]
"""``forward(request, *, max_bytes, timeout)`` to the side's fixed upstream."""


def preview_csp(frame_ancestors: Sequence[str]) -> str:
    ancestors = " ".join(item for item in frame_ancestors if item) or "'none'"
    return (
        "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
        "media-src 'self' data: blob:; connect-src 'self'; worker-src 'self' blob:; frame-src 'self'; "
        f"object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors {ancestors}"
    )


def safe_relative_target(value: str) -> str | None:
    """An origin-relative path (``/x?y#z``) or ``None``: never another origin."""

    if not value or len(value) > 2048 or any(ch in value for ch in "\\\r\n\t\0"):
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    return value


def safe_location(value: str) -> str | None:
    """An upstream ``Location`` kept on the preview origin, or ``None`` (refused).

    Relative references stay relative; absolute URLs are accepted only when
    they name the upstream's own loopback (which is this preview) and are
    rewritten to an origin-relative path. Anything else would be an open
    redirect out of the sandbox.
    """

    if not value or len(value) > 2048 or any(ch in value for ch in "\\\r\n\t\0"):
        return None
    if value.startswith("//"):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        if parts.scheme.lower() not in {"http", "https"}:
            return None
        if (parts.hostname or "").lower() not in _LOOPBACK_NAMES:
            return None
        rebuilt = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        rebuilt += f"#{parts.fragment}" if parts.fragment else ""
        return safe_relative_target(rebuilt)
    if ":" in value.split("/", 1)[0]:
        # ``javascript:``-style references without ``//``.
        return None
    return value


def namespaced_set_cookie(value: str, prefix: str, *, secure: bool) -> str | None:
    """Rewrite an upstream ``Set-Cookie`` into this side's namespace (host-only, ``Path=/``)."""

    name, sep, rest = value.partition("=")
    name = name.strip()
    if not sep or not _COOKIE_NAME.fullmatch(name):
        return None
    cookie_value, *attributes = rest.split(";")
    kept: list[str] = []
    for attribute in attributes:
        key = attribute.split("=", 1)[0].strip().lower()
        if key in _KEPT_COOKIE_ATTRIBUTES and not (key == "secure" and not secure):
            kept.append(attribute.strip())
    if secure and not any(item.lower() == "secure" for item in kept):
        kept.append("Secure")
    return f"{prefix}{name}={cookie_value.strip()}; Path=/" + "".join(f"; {item}" for item in kept)


def _cookie_pairs(header: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for chunk in header.split(";"):
        name, sep, value = chunk.strip().partition("=")
        if sep and name:
            pairs.append((name.strip(), value.strip()))
    return pairs


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def mint_ticket(state: PreviewState, key: str, next_path: str = "/", *, clock: Callable[[], float] = time.time) -> str:
    """A one-time entry ticket (valid :data:`TICKET_TTL_SECONDS`) for one session side.

    Only its digest is kept, in the shared state, so any replica can redeem it
    exactly once.
    """

    ticket = secrets.token_urlsafe(32)
    target = safe_relative_target(next_path) or "/"
    state.put_ticket(
        key, _digest(ticket), target, expires_at=clock() + TICKET_TTL_SECONDS, limit=MAX_OUTSTANDING_TICKETS
    )
    return ticket


def enter_target(ticket: str) -> str:
    """The entry URL path; the ticket rides in the fragment, never sent to any server."""

    return f"{ENTER_PATH}#ticket={quote(ticket, safe='')}"


# The entry page: reads the ticket from the fragment, removes it from the
# address, redeems it with a same-origin POST and replaces itself with the
# ticket's target. It holds no secret of its own.
_ENTER_PAGE: Final[bytes] = (
    b'<!doctype html><html><head><meta charset="utf-8"><title>Opening preview</title></head><body>'
    b'<p id="m">Opening preview\xe2\x80\xa6</p><script>(function(){'
    b"var m=/[#&]ticket=([^&]+)/.exec(location.hash);history.replaceState(null,'',location.pathname);"
    b"var say=function(t){document.getElementById('m').textContent=t;};"
    b"if(!m){say('This preview link is incomplete; open it from the Review.');return;}"
    b"fetch('" + ENTER_PATH.encode() + b"',{method:'POST',credentials:'same-origin',"
    b"headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'ticket='+m[1]})"
    b".then(function(r){return r.ok?r.json():Promise.reject(r.status);})"
    b".then(function(d){location.replace(d.next);})"
    b".catch(function(){say('This preview link was already used or has expired; open it from the Review.');});"
    b"})();</script></body></html>"
)


def _text(status: int, message: str, extra: Sequence[tuple[str, str]] = ()) -> ProxyResponse:
    body = (message.strip() + "\n").encode("utf-8")
    return ProxyResponse(status, (("Content-Type", "text/plain; charset=utf-8"), *extra), body)


class PreviewProxyCore:
    """Transport-independent proxy for one session side.

    ``gate()`` is evaluated on every request; ``forward`` reaches only the
    side's fixed upstream; ``record`` receives one event per served request
    (already-redacted by the caller's recorder). Tickets and capability digests
    live in ``state`` (shared by every replica that can serve this side), so a
    core is rebuilt on any process from the session's shared record.
    """

    def __init__(
        self,
        *,
        session_id: str,
        side: str,
        gate: Callable[[], GateDecision],
        forward: Forward,
        record: Callable[[Mapping[str, Any]], None],
        frame_ancestors: Sequence[str] = (),
        secure_cookies: bool = False,
        clock: Callable[[], float] = time.time,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        upstream_timeout: float = UPSTREAM_TIMEOUT_SECONDS,
        state: PreviewState | None = None,
    ) -> None:
        self.session_id = session_id
        self.side = side
        self._gate = gate
        self._forward = forward
        self._record = record
        self._frame_ancestors = tuple(frame_ancestors)
        self._secure = secure_cookies
        self._clock = clock
        self._max_response = max(1024, int(max_response_bytes))
        self._max_request = max(0, int(max_request_bytes))
        self._timeout = max(0.1, float(upstream_timeout))
        self._state: PreviewState = state if state is not None else InMemoryPreviewState(clock=clock)
        self.key = side_key(session_id, side)
        tag = hashlib.sha256(f"{session_id}\0{side}".encode()).hexdigest()[:12]
        self.cookie_name = f"lcp_{tag}"
        self.app_cookie_prefix = f"lcpa_{tag}_"
        self.closed = False

    # ------------------------------------------------------------------ #
    # entry
    # ------------------------------------------------------------------ #

    def mint_ticket(self, next_path: str = "/") -> str:
        """A one-time entry ticket (valid :data:`TICKET_TTL_SECONDS`) for the session's viewer."""

        return mint_ticket(self._state, self.key, next_path, clock=self._clock)

    def enter_target(self, ticket: str) -> str:
        """The entry URL path; the ticket rides in the fragment, never sent to any server."""

        return enter_target(ticket)

    def _redeem(self, ticket: str) -> str | None:
        row = self._state.take_ticket(self.key, _digest(ticket))
        if row is None or self._clock() >= row[0]:
            return None
        return row[1]

    def _authenticated(self, request: ProxyRequest) -> bool:
        for name, value in _cookie_pairs(request.header("cookie")):
            if name == self.cookie_name and value and self._state.has_capability(self.key, _digest(value)):
                return True
        return False

    # ------------------------------------------------------------------ #
    # handling
    # ------------------------------------------------------------------ #

    def _security_headers(self) -> tuple[tuple[str, str], ...]:
        return (
            ("Content-Security-Policy", preview_csp(self._frame_ancestors)),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("Cache-Control", "no-store"),
            ("Cross-Origin-Resource-Policy", "same-origin"),
            ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=(), clipboard-read=()"),
            ("X-Robots-Tag", "noindex"),
        )

    def _finish(self, response: ProxyResponse) -> ProxyResponse:
        return ProxyResponse(response.status, (*response.headers, *self._security_headers()), response.body)

    def _event(self, request: ProxyRequest, status: int, size: int, kind: str = "request") -> None:
        try:
            self._record(
                {
                    "t": self._clock(),
                    "side": self.side,
                    "kind": kind,
                    "method": request.method[:16],
                    "path": request.target[:2048],
                    "status": int(status),
                    "bytes": int(size),
                }
            )
        except Exception:
            # Recording must never break serving.
            return

    def handle(self, request: ProxyRequest) -> ProxyResponse:
        method = request.method.upper()
        if self.closed:
            return self._finish(_text(410, "This preview session has ended."))
        if method not in ALLOWED_METHODS:
            return self._finish(_text(405, "Method not allowed in a LemonCrow preview."))
        target = request.target
        if (
            not target.startswith("/")
            or target.startswith("//")
            or len(target) > MAX_TARGET_CHARS
            or any(ch in target for ch in "\r\n\0")
        ):
            # Absolute-form (``GET http://169.254.169.254/``) and authority-form
            # targets would turn the proxy into an open forwarder.
            return self._finish(_text(400, "Only origin-relative request targets are served."))
        path = target.split("?", 1)[0]
        if path == ENTER_PATH:
            return self._enter(request)
        if not self._authenticated(request):
            return self._finish(_text(403, "This preview needs its session capability; open it from the Review."))
        decision = self._gate()
        if not decision.allowed:
            return self._finish(
                _text(decision.status or 403, decision.reason or "This preview session is not available.")
            )
        if len(request.body) > self._max_request:
            return self._finish(_text(413, "Request body exceeds the preview limit."))
        upstream = self._upstream_request(request, method)
        try:
            answer = self._forward(upstream, max_bytes=self._max_response, timeout=self._timeout)
        except PreviewResponseTooLarge:
            self._event(request, 502, 0, "response_too_large")
            return self._finish(_text(502, "The preview response exceeded the size limit."))
        except PreviewUpstreamTimeout:
            self._event(request, 504, 0, "upstream_timeout")
            return self._finish(_text(504, "The preview app did not answer in time."))
        except PreviewUpstreamError:
            self._event(request, 502, 0, "upstream_error")
            return self._finish(_text(502, "The preview app is not answering."))
        response = self._downstream_response(answer)
        if response is None:
            self._event(request, 502, 0, "blocked_redirect")
            return self._finish(_text(502, "The preview tried to redirect outside itself; refused."))
        self._event(request, response.status, len(response.body))
        return self._finish(response)

    def _enter(self, request: ProxyRequest) -> ProxyResponse:
        method = request.method.upper()
        if method == "GET":
            if urlsplit(request.target).query:
                # A ticket is never accepted from a URL a log could hold.
                return self._finish(_text(400, "Open this preview from the Review."))
            return self._finish(ProxyResponse(200, (("Content-Type", "text/html; charset=utf-8"),), _ENTER_PAGE))
        if method != "POST":
            return self._finish(_text(405, "Enter with POST."))
        if len(request.body) > 1024:
            return self._finish(_text(413, "Entry request is too large."))
        form = parse_qs(request.body.decode("ascii", errors="replace"), keep_blank_values=False)
        ticket = (form.get("ticket") or [""])[0]
        target = self._redeem(ticket) if ticket else None
        if target is None:
            return self._finish(
                _text(403, "This preview link was already used or has expired; open it from the Review.")
            )
        decision = self._gate()
        if not decision.allowed:
            return self._finish(
                _text(decision.status or 403, decision.reason or "This preview session is not available.")
            )
        attributes = "Path=/; HttpOnly; " + (
            "Secure; SameSite=None; Partitioned" if self._secure else "SameSite=Strict"
        )
        # A fresh capability per redeemed ticket; only its digest is shared.
        capability = secrets.token_urlsafe(32)
        self._state.add_capability(self.key, _digest(capability), ttl=CAPABILITY_TTL_SECONDS, limit=MAX_CAPABILITIES)
        body = json.dumps({"next": target}).encode("utf-8")
        return self._finish(
            ProxyResponse(
                200,
                (
                    ("Content-Type", "application/json"),
                    ("Set-Cookie", f"{self.cookie_name}={capability}; {attributes}"),
                ),
                body,
            )
        )

    def _upstream_request(self, request: ProxyRequest, method: str) -> ProxyRequest:
        headers: list[tuple[str, str]] = []
        for name, value in request.headers:
            if name.lower() in _REQUEST_HEADERS and "\n" not in value and "\r" not in value:
                headers.append((name, value))
        # Only cookies this side's app set itself, under its namespace.
        app_cookies = [
            f"{name[len(self.app_cookie_prefix):]}={value}"
            for name, value in _cookie_pairs(request.header("cookie"))
            if name.startswith(self.app_cookie_prefix) and len(name) > len(self.app_cookie_prefix)
        ]
        if app_cookies:
            headers.append(("Cookie", "; ".join(app_cookies)))
        if request.header("origin"):
            headers.append(("Origin", UPSTREAM_ORIGIN))
        headers.extend((("Host", "localhost"), ("Accept-Encoding", "identity"), ("Connection", "close")))
        if request.body or method in {"POST", "PUT", "PATCH"}:
            headers.append(("Content-Length", str(len(request.body))))
        return ProxyRequest(method, request.target, tuple(headers), request.body)

    def _downstream_response(self, answer: ProxyResponse) -> ProxyResponse | None:
        headers: list[tuple[str, str]] = []
        cookies = 0
        for name, value in answer.headers:
            lower = name.lower()
            if "\n" in value or "\r" in value:
                continue
            if lower in _RESPONSE_HEADERS:
                headers.append((name, value))
            elif lower == "location":
                safe = safe_location(value.strip())
                if safe is None:
                    return None
                headers.append(("Location", safe))
            elif lower == "set-cookie" and cookies < MAX_SET_COOKIES:
                rewritten = namespaced_set_cookie(value, self.app_cookie_prefix, secure=self._secure)
                if rewritten is not None:
                    cookies += 1
                    headers.append(("Set-Cookie", rewritten))
        status = int(answer.status)
        if status < 100 or status > 599:
            return None
        headers.append(("Content-Length", str(len(answer.body))))
        return ProxyResponse(status, tuple(headers), answer.body)


# --------------------------------------------------------------------------- #
# Local exposure: one loopback listener (origin) per side
# --------------------------------------------------------------------------- #


class _IPv6ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6


class LoopbackPreviewServer:
    """Serve one :class:`PreviewProxyCore` on its own origin: ``127.0.0.1`` by default.

    A Workspaces server on a private network binds its configured private address instead and names the host the
    person's browser uses (``advertise``, that address by default); only that ``Host`` is served.
    """

    def __init__(self, core: PreviewProxyCore, *, host: str = "127.0.0.1", advertise: str = "") -> None:
        self.core = core
        self._host = host
        self._advertise = advertise.strip().lower()
        handler = _loopback_handler(core, self)
        server_class: type[http.server.ThreadingHTTPServer] = http.server.ThreadingHTTPServer
        if ":" in host:
            server_class = _IPv6ThreadingHTTPServer
        self._server = server_class((host, 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="lc-interactive-preview", daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def _authority(self) -> str:
        name = self._advertise or self._host
        return f"[{name}]:{self.port}" if ":" in name else f"{name}:{self.port}"

    @property
    def origin(self) -> str:
        if self._host in {"127.0.0.1", "localhost"} and not self._advertise:
            return f"http://127.0.0.1:{self.port}"
        return f"http://{self._authority()}"

    def hosts(self) -> tuple[str, ...]:
        if self._host in {"127.0.0.1", "localhost"} and not self._advertise:
            return (f"127.0.0.1:{self.port}", f"localhost:{self.port}")
        return (self._authority(),)

    def close(self) -> None:
        self.core.closed = True
        self._server.shutdown()
        self._server.server_close()


def _loopback_handler(core: PreviewProxyCore, owner: LoopbackPreviewServer) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "LemonCrowInteractivePreview/1"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            # The request line carries entry tickets: never log it.
            return

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            return

        def _serve(self) -> None:
            if self.headers.get("Host", "") not in owner.hosts():
                # DNS-rebinding defence: only the literal loopback origin.
                self._write(_text(421, "Misdirected preview request."), head=False)
                return
            if self.headers.get("Transfer-Encoding"):
                self._write(_text(411, "A Content-Length is required."), head=False)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_REQUEST_BYTES:
                self._write(_text(413, "Request body exceeds the preview limit."), head=False)
                self.close_connection = True
                return
            body = self.rfile.read(length) if length else b""
            request = ProxyRequest(self.command, self.path, tuple(self.headers.items()), body)
            self._write(core.handle(request), head=self.command == "HEAD")

        def _write(self, response: ProxyResponse, *, head: bool) -> None:
            self.send_response(response.status)
            names = {name.lower() for name, _ in response.headers}
            for name, value in response.headers:
                self.send_header(name, value)
            if "content-length" not in names:
                self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            if not head and response.body:
                self.wfile.write(response.body)

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _serve

        def do_CONNECT(self) -> None:
            self._write(core.handle(ProxyRequest("CONNECT", self.path, tuple(self.headers.items()))), head=False)

        do_TRACE = do_CONNECT

    return Handler


__all__ = [
    "ALLOWED_METHODS",
    "CAPABILITY_TTL_SECONDS",
    "ENTER_PATH",
    "MAX_CAPABILITIES",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "TICKET_TTL_SECONDS",
    "UPSTREAM_ORIGIN",
    "GateDecision",
    "LoopbackPreviewServer",
    "PreviewProxyCore",
    "PreviewResponseTooLarge",
    "PreviewUpstreamError",
    "PreviewUpstreamTimeout",
    "ProxyRequest",
    "ProxyResponse",
    "enter_target",
    "mint_ticket",
    "namespaced_set_cookie",
    "preview_csp",
    "safe_location",
    "safe_relative_target",
]
