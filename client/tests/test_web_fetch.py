from __future__ import annotations

import http.client
import socket
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest
from lemoncrow_client import webtransport
from lemoncrow_client.dispatcher import Dispatcher
from lemoncrow_client.localtools import LocalContext
from lemoncrow_client.localtools.web import _Markdown, run_web_fetch


@pytest.fixture
def origin() -> Iterator[tuple[str, list[Any]]]:
    calls: list[Any] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            calls.append((self.path, self.headers))
            status, body, kind = 200, b"# Local page\n\nClient fetch works.", "text/markdown"
            if self.path in {"/redirect", "/private"}:
                status, body = 302, b"Unread redirect body"
            elif self.path == "/html":
                body, kind = b'<nav>noise</nav><h1>Title</h1><p>Hello <a href="/link">world</a></p>', "text/html"
            elif self.path == "/missing":
                status = 404
            elif self.path == "/large":
                body = b"x" * 40_000
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            if status == 302:
                self.send_header("Location", "/page" if self.path == "/redirect" else "http://10.0.0.1/")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def context(config: Any) -> LocalContext:
    return LocalContext(config, config.repo_root, environment={"LEMONCROW_WEB_FETCH_ALLOW_LOOPBACK": "1"})


def test_dispatch_is_local(origin: Any, config: Any, bootstrapped: Any, stub: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("LEMONCROW_WEB_FETCH_ALLOW_LOOPBACK", "1")
    dispatcher = Dispatcher(config, bootstrapped)
    monkeypatch.setattr(type(bootstrapped), "call_tool", lambda *_a, **_k: pytest.fail("server RPC"))
    outcome = dispatcher.call("web_fetch", {"url": origin[0] + "/redirect"})
    assert not outcome.is_error, outcome.content
    assert outcome.site == "client"
    assert not outcome.degraded
    assert "Client fetch works." in outcome.content[0]["text"]
    assert [path for path, _ in origin[1]] == ["/redirect", "/page"]
    assert all(headers.get("Authorization") is None for _, headers in origin[1])


def test_no_server_required_and_cache_is_session_scoped(origin: Any, config: Any) -> None:
    ctx = context(config)
    for _ in range(2):
        assert not run_web_fetch(ctx, {"url": origin[0] + "/page"}).is_error
    assert len(origin[1]) == 1
    assert not run_web_fetch(context(config), {"url": origin[0] + "/page"}).is_error
    assert len(origin[1]) == 2


def test_html_and_error_status(origin: Any, config: Any) -> None:
    result = run_web_fetch(context(config), {"url": origin[0] + "/html"})
    assert "# Title" in result.content[0]["text"]
    assert f"[world]({origin[0]}/link)" in result.content[0]["text"]
    assert "noise" not in result.content[0]["text"]
    # The full server's credit: the page's HTML against the markdown sent.
    html = '<nav>noise</nav><h1>Title</h1><p>Hello <a href="/link">world</a></p>'
    assert result.tokens_saved == max(0, len(html) // 4 - len(result.structured["content"]) // 4)
    missing = run_web_fetch(context(config), {"url": origin[0] + "/missing"})
    assert missing.tokens_saved == 0  # markdown, not HTML
    assert not missing.is_error
    assert missing.structured["status"] == 404


def test_private_redirect_blocked(origin: Any, config: Any) -> None:
    result = run_web_fetch(context(config), {"url": origin[0] + "/private"})
    assert result.is_error
    assert "private/local" in result.content[0]["text"]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:secret@example.com", "http://x/\x00"])
def test_invalid_urls_never_connect(url: str, config: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("opened connection"))
    assert run_web_fetch(context(config), {"url": url}).is_error


def test_mixed_public_private_dns_blocked(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80)),
        ],
    )
    with pytest.raises(ValueError, match="private/local"):
        webtransport._public_addresses("example.com", 80, allow_loopback=False)


def test_transport_retries_connection_state_failure(monkeypatch: Any) -> None:
    calls = []
    page = webtransport.Page("https://example.com", 200, "text/plain", b"ok")

    def once(*_a: Any, **_k: Any) -> webtransport.Page:
        calls.append(1)
        if len(calls) == 1:
            raise http.client.ResponseNotReady("Request-sent")
        return page

    monkeypatch.setattr(webtransport, "_once", once)
    monkeypatch.setattr(webtransport.time, "sleep", lambda _: None)
    assert webtransport.fetch_page("https://example.com", accept="text/plain") == page
    assert len(calls) == 2


def test_summary_spills_full_page(origin: Any, config: Any, tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path))
    result = run_web_fetch(context(config), {"url": origin[0] + "/large", "summary": True})
    assert not result.is_error
    assert "summarized:heuristic" in result.content[0]["text"]
    paths = list(tmp_path.glob("web-fetch-*.md"))
    assert len(paths) == 1
    assert len(paths[0].read_text()) == 40_000


def test_parser_preserves_code_whitespace() -> None:
    parser = _Markdown("https://example.com")
    parser.feed("<pre><code>if ok:\n    run()\n</code></pre>")
    assert "if ok:\n    run()" in parser.result()


@pytest.mark.parametrize("limit, succeeds", [(40_000, True), (39_999, False)])
def test_body_cap_at_and_past_boundary(origin: Any, monkeypatch: Any, limit: int, succeeds: bool) -> None:
    monkeypatch.setattr(webtransport, "MAX_BODY_BYTES", limit)
    if succeeds:
        page = webtransport.fetch_page(origin[0] + "/large", accept="text/plain", allow_loopback=True)
        assert len(page.body) == limit
    else:
        with pytest.raises(ValueError, match="body exceeds"):
            webtransport.fetch_page(origin[0] + "/large", accept="text/plain", allow_loopback=True)
