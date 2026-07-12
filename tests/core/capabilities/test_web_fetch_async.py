"""Phase 3 async web_fetch: SSRF guard parity + output parity with the sync path.

These exercise the security boundary of the aiohttp path directly:
- bare-IP targets are validated at the connection level (aiohttp bypasses the
  resolver for literal IPs), and
- hostnames go through ``_ValidatingResolver`` which returns ONLY validated IPs
  (so a name resolving to a private address is rejected -> no DNS rebinding).
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from lemoncrow.core.capabilities import web_fetch


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Loopback fetches are denied by default; these tests run real loopback
    # HTTP servers, so opt in explicitly.
    monkeypatch.setenv("LEMONCROW_WEB_FETCH_ALLOW_LOOPBACK", "1")
    web_fetch.clear_web_fetch_cache()
    yield
    web_fetch.clear_web_fetch_cache()


def _loopback_server(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _fake_getaddrinfo(ip: str) -> Callable[..., list[tuple[Any, ...]]]:
    def _gai(
        host: str, port: int, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
    ) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    return _gai


class _TextHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"hello from async"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a: Any) -> None:
        pass


class _RedirectToPrivateHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(302)
        self.send_header("Location", "http://10.0.0.1/internal")
        self.end_headers()

    def log_message(self, *_a: Any) -> None:
        pass


class _BinaryHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"\x00\x01\x02"
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a: Any) -> None:
        pass


class _PdfHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        from tests.core.capabilities.test_web_fetch import _build_minimal_pdf

        body = _build_minimal_pdf("Hello PDF")
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a: Any) -> None:
        pass


class _FlakyForbiddenHandler(BaseHTTPRequestHandler):
    """403 on the first hit (simulated WAF false positive), 200 after."""

    calls: ClassVar[int] = 0

    def do_GET(self) -> None:
        type(self).calls += 1
        if type(self).calls == 1:
            body = b"forbidden"
            self.send_response(403)
        else:
            body = b"ok now"
            self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a: Any) -> None:
        pass


# --------------------------------------------------------------------------- #
# SSRF: literal private / link-local IP targets blocked at connection level   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
    ],
)
def test_async_blocks_private_ip_literal(url: str) -> None:
    with pytest.raises(ValueError, match="private/local"):
        asyncio.run(web_fetch.async_fetch_url(url, output_format="text"))


# --------------------------------------------------------------------------- #
# SSRF: hostname -> private resolution rejected by the validating resolver     #
# (closes the DNS-rebinding gap: only validated IPs are returned to aiohttp)    #
# --------------------------------------------------------------------------- #


def test_async_resolver_rejects_hostname_resolving_to_private(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("10.1.2.3"))

    async def _run() -> Any:
        return await web_fetch._ValidatingResolver().resolve("evil.example.com", 80, socket.AF_INET)

    with pytest.raises(ValueError, match="private/local"):
        asyncio.run(_run())


def test_async_resolver_returns_only_validated_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("8.8.8.8"))

    async def _run() -> Any:
        return await web_fetch._ValidatingResolver().resolve("example.com", 443, socket.AF_INET)

    results = asyncio.run(_run())
    assert len(results) == 1
    assert results[0]["host"] == "8.8.8.8"
    # Hostname preserved so TLS SNI + cert verification + Host header stay correct.
    assert results[0]["hostname"] == "example.com"


def test_async_resolver_allows_loopback_literal() -> None:
    async def _run() -> Any:
        return await web_fetch._ValidatingResolver().resolve("127.0.0.1", 80, socket.AF_INET)

    results = asyncio.run(_run())
    assert results[0]["host"] == "127.0.0.1"


def test_async_resolver_blocks_loopback_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_WEB_FETCH_ALLOW_LOOPBACK", raising=False)

    async def _run() -> Any:
        return await web_fetch._ValidatingResolver().resolve("127.0.0.1", 80, socket.AF_INET)

    with pytest.raises(ValueError, match="LEMONCROW_WEB_FETCH_ALLOW_LOOPBACK"):
        asyncio.run(_run())


# --------------------------------------------------------------------------- #
# SSRF: a redirect to a private IP is blocked on the next hop                  #
# --------------------------------------------------------------------------- #


def test_async_blocks_redirect_to_private_ip() -> None:
    srv, port = _loopback_server(_RedirectToPrivateHandler)
    try:
        with pytest.raises(ValueError, match="private/local"):
            asyncio.run(web_fetch.async_fetch_url(f"http://127.0.0.1:{port}/", output_format="text"))
    finally:
        srv.shutdown()
        srv.server_close()


def test_async_rejects_binary_content_type() -> None:
    srv, port = _loopback_server(_BinaryHandler)
    try:
        with pytest.raises(ValueError, match="unsupported content type"):
            asyncio.run(web_fetch.async_fetch_url(f"http://127.0.0.1:{port}/", output_format="text"))
    finally:
        srv.shutdown()
        srv.server_close()


def test_async_retries_transient_403_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 403 is a possible WAF/bot-management false positive: retried a bounded
    number of times on the async path too before being accepted as final."""
    monkeypatch.setattr(web_fetch, "_RETRY_BACKOFF_S", 0.0)
    _FlakyForbiddenHandler.calls = 0
    srv, port = _loopback_server(_FlakyForbiddenHandler)
    try:
        result = asyncio.run(web_fetch._async_fetch_uncached(f"http://127.0.0.1:{port}/", accept="*/*", timeout_s=5.0))
    finally:
        srv.shutdown()
        srv.server_close()
    assert result.status == 200
    assert _FlakyForbiddenHandler.calls == 2


def test_async_accepts_and_extracts_pdf() -> None:
    srv, port = _loopback_server(_PdfHandler)
    try:
        result = asyncio.run(web_fetch.async_fetch_url(f"http://127.0.0.1:{port}/", output_format="text"))
    finally:
        srv.shutdown()
        srv.server_close()
    assert result["content"].startswith("Hello PDF")
    assert "downloaded PDF:" in result["content"]
    assert result["format"] == "text"


# --------------------------------------------------------------------------- #
# Parity: async output == sync output for the same loopback resource           #
# --------------------------------------------------------------------------- #


def test_async_loopback_parity_with_sync() -> None:
    srv, port = _loopback_server(_TextHandler)
    try:
        url = f"http://127.0.0.1:{port}/"
        web_fetch.clear_web_fetch_cache()
        async_result = asyncio.run(web_fetch.async_fetch_url(url, output_format="text"))
        web_fetch.clear_web_fetch_cache()
        sync_result = web_fetch.fetch_url(url, output_format="text")
    finally:
        srv.shutdown()
        srv.server_close()
    assert async_result == sync_result
    assert async_result["content"] == "hello from async"


class _MarkdownHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"# Heading One\n\nSome prose about the page.\n\n## Heading Two\n\nMore prose about another section.\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a: Any) -> None:
        pass


def test_async_fetch_url_carries_summary_param(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The deferred/async execution path threads `summary` all the way down
    to `_finish_fetch`, same as the sync path."""
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_LLM_BACKEND", raising=False)
    srv, port = _loopback_server(_MarkdownHandler)
    try:
        result = asyncio.run(
            web_fetch.async_fetch_url(f"http://127.0.0.1:{port}/", output_format="markdown", summary=True)
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert "# Heading One" in result["content"]
    assert "## Heading Two" in result["content"]
    assert "[lc: summarized:heuristic" in result["content"]
