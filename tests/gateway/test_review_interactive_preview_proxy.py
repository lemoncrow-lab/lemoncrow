"""Proxy hardening for interactive previews (Phase 9R item 16, security bullets)."""

from __future__ import annotations

import http.client
import json
import logging
import shutil
import socket
import socketserver
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from lemoncrow.pro.capabilities.review.preview_runtime import forward_unix
from lemoncrow.pro.environment.preview_proxy import (
    ENTER_PATH,
    TICKET_TTL_SECONDS,
    GateDecision,
    LoopbackPreviewServer,
    PreviewProxyCore,
    PreviewResponseTooLarge,
    PreviewUpstreamError,
    PreviewUpstreamTimeout,
    ProxyRequest,
    ProxyResponse,
    safe_location,
)

ANCESTOR = "http://127.0.0.1:45990"


class Upstream:
    def __init__(self, response: ProxyResponse | Exception | None = None) -> None:
        self.response = response or ProxyResponse(200, (("Content-Type", "text/html"),), b"<p>ok</p>")
        self.requests: list[ProxyRequest] = []

    def __call__(self, request: ProxyRequest, *, max_bytes: int, timeout: float) -> ProxyResponse:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class Harness:
    def __init__(self, *, secure: bool = False, max_request_bytes: int = 1024 * 1024) -> None:
        self.now = 1_800_000_000.0
        self.gate = GateDecision(True)
        self.upstream = Upstream()
        self.events: list[dict[str, Any]] = []
        self.core = PreviewProxyCore(
            session_id="ips_test",
            side="new",
            gate=lambda: self.gate,
            forward=self.upstream,
            record=lambda event: self.events.append(dict(event)),
            frame_ancestors=(ANCESTOR,),
            secure_cookies=secure,
            clock=lambda: self.now,
            max_request_bytes=max_request_bytes,
        )

    def redeem(self, ticket: str) -> ProxyResponse:
        return self.core.handle(ProxyRequest("POST", ENTER_PATH, (), f"ticket={ticket}".encode()))

    def enter(self, next_path: str = "/") -> tuple[ProxyResponse, str]:
        response = self.redeem(self.core.mint_ticket(next_path))
        cookie = response.header("Set-Cookie").split(";", 1)[0]
        return response, cookie

    def get(self, target: str, *, cookie: str = "", method: str = "GET", **headers: str) -> ProxyResponse:
        pairs = [("Cookie", cookie)] if cookie else []
        pairs.extend((name.replace("_", "-"), value) for name, value in headers.items())
        return self.core.handle(ProxyRequest(method, target, tuple(pairs)))


@pytest.fixture
def proxy() -> Harness:
    return Harness()


# --------------------------------------------------------------------------- #
# SSRF: the upstream is fixed; request targets can never steer it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://[fd00:ec2::254]/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://127.0.0.1:22/",
        "//10.0.0.1/admin",
        "169.254.169.254:80",
        "*",
    ],
)
def test_absolute_and_authority_form_targets_never_reach_the_upstream(proxy: Harness, target: str) -> None:
    _entered, cookie = proxy.enter()
    response = proxy.get(target, cookie=cookie)
    assert response.status == 400
    assert proxy.upstream.requests == []


@pytest.mark.parametrize("method", ["CONNECT", "TRACE", "PROPFIND"])
def test_tunnel_and_trace_methods_are_refused(proxy: Harness, method: str) -> None:
    _entered, cookie = proxy.enter()
    assert proxy.get("/", cookie=cookie, method=method).status == 405
    assert proxy.upstream.requests == []


# --------------------------------------------------------------------------- #
# capability and one-time entry
# --------------------------------------------------------------------------- #


def test_requests_without_the_capability_are_refused(proxy: Harness) -> None:
    assert proxy.get("/").status == 403
    assert proxy.get("/", cookie=f"{proxy.core.cookie_name}=guess").status == 403
    assert proxy.upstream.requests == []


def test_entry_ticket_is_single_use_and_sets_an_httponly_capability(proxy: Harness) -> None:
    ticket = proxy.core.mint_ticket("/settings?x=1")
    first = proxy.redeem(ticket)
    assert first.status == 200
    assert json.loads(first.body) == {"next": "/settings?x=1"}
    cookie = first.header("Set-Cookie")
    assert cookie.startswith(proxy.core.cookie_name + "=")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert "Domain" not in cookie
    assert proxy.redeem(ticket).status == 403
    assert proxy.get("/", cookie=cookie.split(";", 1)[0]).status == 200


def test_entry_ticket_expires_on_the_clock(proxy: Harness) -> None:
    ticket = proxy.core.mint_ticket()
    proxy.now += TICKET_TTL_SECONDS + 1
    assert proxy.redeem(ticket).status == 403


def test_ticket_is_never_accepted_from_a_url(proxy: Harness) -> None:
    ticket = proxy.core.mint_ticket()
    for target in (f"{ENTER_PATH}?ticket={ticket}", f"{ENTER_PATH}?x=1"):
        response = proxy.core.handle(ProxyRequest("GET", target))
        assert response.status == 400 and not response.header("Set-Cookie")
    page = proxy.core.handle(ProxyRequest("GET", ENTER_PATH))
    assert page.status == 200 and b"location.hash" in page.body and not page.header("Set-Cookie")
    # The URL attempt did not consume the ticket; it still redeems once by POST.
    assert proxy.redeem(ticket).status == 200


def test_entry_cannot_redirect_off_the_preview(proxy: Harness) -> None:
    for evil in ("https://evil.example/", "//evil.example/", "javascript:alert(1)", "/\\evil.example"):
        response, _cookie = proxy.enter(evil)
        assert json.loads(response.body) == {"next": "/"}


def test_hosted_capability_cookie_is_secure_and_partitioned() -> None:
    harness = Harness(secure=True)
    response, _cookie = harness.enter()
    cookie = response.header("Set-Cookie")
    assert "Secure" in cookie and "SameSite=None" in cookie and "Partitioned" in cookie and "HttpOnly" in cookie


def test_gate_is_checked_on_every_request(proxy: Harness) -> None:
    _entered, cookie = proxy.enter()
    assert proxy.get("/", cookie=cookie).status == 200
    proxy.gate = GateDecision(False, 410, "This preview session has ended (time limit reached).")
    refused = proxy.get("/", cookie=cookie)
    assert refused.status == 410
    assert b"time limit" in refused.body
    assert len(proxy.upstream.requests) == 1
    # A fresh ticket for an ended session is refused too.
    assert proxy.redeem(proxy.core.mint_ticket()).status == 410


# --------------------------------------------------------------------------- #
# header strip/set, cookies, redirects, CSP
# --------------------------------------------------------------------------- #


def test_request_headers_are_allowlisted_and_credentials_never_forwarded(proxy: Harness) -> None:
    _entered, cookie = proxy.enter()
    app_cookie = f"{proxy.core.app_cookie_prefix}sid=app-value"
    proxy.get(
        "/x",
        cookie=f"{cookie}; lemoncrow_session=reader-secret; {app_cookie}",
        Authorization="Bearer reader-token",
        X_Forwarded_For="10.0.0.1",
        Origin="http://127.0.0.1:45990",
        Accept="text/html",
        Proxy_Authorization="Basic x",
    )
    (forwarded,) = proxy.upstream.requests
    names = {name.lower() for name, _ in forwarded.headers}
    assert "authorization" not in names and "x-forwarded-for" not in names and "proxy-authorization" not in names
    assert forwarded.header("Cookie") == "sid=app-value"
    assert "reader-secret" not in repr(forwarded.headers)
    assert proxy.core.cookie_name not in repr(forwarded.headers)
    assert forwarded.header("Host") == "localhost"
    assert forwarded.header("Origin") == "http://localhost"
    assert forwarded.header("Accept") == "text/html"


def test_response_headers_are_stripped_and_hardened(proxy: Harness) -> None:
    proxy.upstream.response = ProxyResponse(
        200,
        (
            ("Content-Type", "text/html"),
            ("Access-Control-Allow-Origin", "*"),
            ("Server", "leaky/1.0"),
            ("Set-Cookie", "sid=abc; Domain=example.com; Path=/admin; HttpOnly"),
            ("Content-Security-Policy", "default-src *"),
        ),
        b"<p>ok</p>",
    )
    _entered, cookie = proxy.enter()
    response = proxy.get("/", cookie=cookie)
    names = [name.lower() for name, _ in response.headers]
    assert "access-control-allow-origin" not in names and "server" not in names
    assert response.header_values("Content-Security-Policy") == [response.header("Content-Security-Policy")]
    csp = response.header("Content-Security-Policy")
    assert "connect-src 'self'" in csp and f"frame-ancestors {ANCESTOR}" in csp and "object-src 'none'" in csp
    (set_cookie,) = response.header_values("Set-Cookie")
    assert set_cookie.startswith(f"{proxy.core.app_cookie_prefix}sid=abc; Path=/")
    assert "Domain" not in set_cookie and "/admin" not in set_cookie
    assert response.header("X-Content-Type-Options") == "nosniff"
    assert response.header("Referrer-Policy") == "no-referrer"
    assert response.header("Cache-Control") == "no-store"


@pytest.mark.parametrize(
    "location",
    ["https://evil.example/", "//evil.example/x", "http://169.254.169.254/", "javascript:alert(1)", "/\\evil"],
)
def test_upstream_redirects_off_the_preview_are_refused(proxy: Harness, location: str) -> None:
    proxy.upstream.response = ProxyResponse(302, (("Location", location),), b"")
    _entered, cookie = proxy.enter()
    response = proxy.get("/login", cookie=cookie)
    assert response.status == 502
    assert response.header("Location") == ""
    assert proxy.events[-1]["kind"] == "blocked_redirect"


def test_upstream_self_redirects_stay_relative(proxy: Harness) -> None:
    assert safe_location("http://localhost:8080/next?a=1") == "/next?a=1"
    assert safe_location("/next") == "/next"
    assert safe_location("next") == "next"
    proxy.upstream.response = ProxyResponse(302, (("Location", "http://127.0.0.1:8080/home"),), b"")
    _entered, cookie = proxy.enter()
    response = proxy.get("/", cookie=cookie)
    assert response.status == 302 and response.header("Location") == "/home"


# --------------------------------------------------------------------------- #
# size and time caps
# --------------------------------------------------------------------------- #


def test_request_body_cap() -> None:
    harness = Harness(max_request_bytes=16)
    _entered, cookie = harness.enter()
    response = harness.core.handle(ProxyRequest("POST", "/", (("Cookie", cookie),), b"x" * 17))
    assert response.status == 413
    assert harness.upstream.requests == []


@pytest.mark.parametrize(
    ("error", "status", "kind"),
    [
        (PreviewResponseTooLarge("big"), 502, "response_too_large"),
        (PreviewUpstreamTimeout("slow"), 504, "upstream_timeout"),
        (PreviewUpstreamError("down"), 502, "upstream_error"),
    ],
)
def test_upstream_failures_map_to_bounded_errors(proxy: Harness, error: Exception, status: int, kind: str) -> None:
    proxy.upstream.response = error
    _entered, cookie = proxy.enter()
    assert proxy.get("/", cookie=cookie).status == status
    assert proxy.events[-1]["kind"] == kind


class _UnixHTTP(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def unix_upstream() -> Iterator[tuple[Path, dict[str, Any]]]:
    behaviour: dict[str, Any] = {"body": b"ok", "delay": 0.0}

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                pass
            body = behaviour["body"]
            self.wfile.write(b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body))
            for start in range(0, len(body), 65536):
                if behaviour["delay"]:
                    time.sleep(behaviour["delay"])
                self.wfile.write(body[start : start + 65536])

    # A unix socket path is limited to 104 bytes on macOS, which pytest's tmp_path exceeds.
    short = Path(tempfile.mkdtemp(prefix="lcup"))
    path = short / "up.sock"
    server = _UnixHTTP(str(path), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield path, behaviour
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(short, ignore_errors=True)


def test_forward_caps_response_size(unix_upstream: tuple[Path, dict[str, Any]]) -> None:
    path, behaviour = unix_upstream
    request = ProxyRequest("GET", "/", (("Host", "localhost"),))
    assert forward_unix(path, request, max_bytes=1024, timeout=5).body == b"ok"
    behaviour["body"] = b"x" * 4096
    with pytest.raises(PreviewResponseTooLarge):
        forward_unix(path, request, max_bytes=1024, timeout=5)


def test_forward_caps_total_time(unix_upstream: tuple[Path, dict[str, Any]]) -> None:
    path, behaviour = unix_upstream
    behaviour["body"] = b"x" * (65536 * 4)
    behaviour["delay"] = 0.4
    started = time.monotonic()
    with pytest.raises(PreviewUpstreamTimeout):
        forward_unix(path, ProxyRequest("GET", "/", (("Host", "localhost"),)), max_bytes=1 << 30, timeout=0.5)
    assert time.monotonic() - started < 3


# --------------------------------------------------------------------------- #
# loopback origin: Host check (DNS rebinding), framing, no logs
# --------------------------------------------------------------------------- #


def _send(port: int, raw: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(raw)
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
            if b"\r\n\r\n" in b"".join(chunks):
                break
        return b"".join(chunks)


def test_loopback_origin_refuses_foreign_host_and_chunked_bodies() -> None:
    harness = Harness()
    server = LoopbackPreviewServer(harness.core)
    try:
        rebinding = _send(server.port, b"GET / HTTP/1.1\r\nHost: attacker.example\r\n\r\n")
        assert rebinding.startswith(b"HTTP/1.1 421")
        chunked = _send(
            server.port,
            f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n".encode(),
        )
        assert chunked.startswith(b"HTTP/1.1 411")
        absolute = _send(
            server.port,
            f"GET http://169.254.169.254/ HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n\r\n".encode(),
        )
        assert absolute.startswith(b"HTTP/1.1 400")
        assert harness.upstream.requests == []
    finally:
        server.close()


def test_tickets_and_capabilities_never_reach_logs(capfd: pytest.CaptureFixture[str], caplog: Any) -> None:
    caplog.set_level(logging.DEBUG)
    harness = Harness()
    server = LoopbackPreviewServer(harness.core)
    try:
        ticket = harness.core.mint_ticket("/")
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        host = {"Host": f"127.0.0.1:{server.port}"}
        conn.request("GET", ENTER_PATH, headers=host)
        conn.getresponse().read()
        conn.request("POST", ENTER_PATH, body=f"ticket={ticket}", headers=host)
        response = conn.getresponse()
        response.read()
        capability = (response.getheader("Set-Cookie") or "").split(";", 1)[0]
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{server.port}", "Cookie": capability})
        conn.getresponse().read()
        conn.close()
    finally:
        server.close()
    out, err = capfd.readouterr()
    secret = capability.split("=", 1)[1]
    for text in (out, err, caplog.text):
        assert ticket not in text
        assert secret not in text
        assert ENTER_PATH not in text


def test_enter_target_carries_the_ticket_only_in_the_fragment(proxy: Harness) -> None:
    target = proxy.core.enter_target(proxy.core.mint_ticket("/a"))
    parts = urlsplit(target)
    assert parts.path == ENTER_PATH
    assert parts.query == ""
    assert set(parse_qs(parts.fragment)) == {"ticket"}
