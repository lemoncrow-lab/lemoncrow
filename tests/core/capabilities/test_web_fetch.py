from __future__ import annotations

import concurrent.futures
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, ClassVar

import pytest

import lemoncrow.infra.internal_llm as internal_llm
from lemoncrow.core.capabilities import web_fetch


class _FakeResponse:
    status = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "text/markdown; charset=utf-8"}

    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, amt: int = 65536, decode_content: bool = True) -> Iterator[bytes]:
        _ = (amt, decode_content)
        yield self._body

    def release_conn(self) -> None:
        return None


class _FakeRedirectResponse:
    status = 302
    headers: ClassVar[dict[str, str]] = {}

    def __init__(self, location: str) -> None:
        self.headers = {"location": location}

    def stream(self, amt: int = 65536, decode_content: bool = True) -> Iterator[bytes]:
        return iter([b""])

    def release_conn(self) -> None:
        return None


class _FakeErrorResponse:
    headers: ClassVar[dict[str, str]] = {"content-type": "text/plain; charset=utf-8"}

    def __init__(self, body: bytes = b"Not Found", status: int = 404) -> None:
        self._body = body
        self.status = status

    def stream(self, amt: int = 65536, decode_content: bool = True) -> Iterator[bytes]:
        yield self._body

    def release_conn(self) -> None:
        return None


class _FakeBinaryResponse:
    status = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "application/octet-stream"}

    def __init__(self) -> None:
        self._body = b"\x00\x01\x02"

    def stream(self, amt: int = 65536, decode_content: bool = True) -> Iterator[bytes]:
        yield self._body

    def release_conn(self) -> None:
        return None


def _build_minimal_pdf(text: str = "Hello PDF") -> bytes:
    """Build a minimal single-page PDF containing *text*, using only pypdf."""
    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 24 Tf 20 100 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(content)
    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    resources = DictionaryObject()
    font_dict = DictionaryObject()
    font_dict[NameObject("/F1")] = writer._add_object(font)
    resources[NameObject("/Font")] = font_dict
    page[NameObject("/Resources")] = resources
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class _FakePdfResponse:
    status = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "application/pdf"}

    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, amt: int = 65536, decode_content: bool = True) -> Iterator[bytes]:
        yield self._body

    def release_conn(self) -> None:
        return None


class _FakeHTTP:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, *args: object, **kwargs: object) -> _FakeResponse:
        self.calls += 1
        return _FakeResponse(b"# Cached\n\nBody")


@pytest.fixture(autouse=True)
def clear_cache() -> Iterator[None]:
    web_fetch.clear_web_fetch_cache()
    yield
    web_fetch.clear_web_fetch_cache()


# --------------------------------------------------------------------------- #
# URL validation (format — no DNS)                                            #
# --------------------------------------------------------------------------- #


def test_validate_url_rejects_control_chars() -> None:
    with pytest.raises(ValueError, match="control characters"):
        web_fetch._validate_public_url("http://example.com/\x00")


def test_validate_url_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        web_fetch._validate_public_url("")


def test_validate_url_rejects_no_hostname() -> None:
    with pytest.raises(ValueError, match="hostname"):
        web_fetch._validate_public_url("http:///path")


def test_validate_url_rejects_credentials() -> None:
    with pytest.raises(ValueError, match="credentials"):
        web_fetch._validate_public_url("http://user:pass@example.com")


def test_validate_url_rejects_bad_scheme() -> None:
    with pytest.raises(ValueError, match="only supports http and https"):
        web_fetch._validate_public_url("ftp://example.com")


def test_validate_url_accepts_loopback_url_format_only() -> None:
    """Format check alone should accept loopback — IP validation happens at connect time."""
    result = web_fetch._validate_public_url("http://127.0.0.1")
    assert result == "http://127.0.0.1"


def test_validate_url_accepts_standard_ports() -> None:
    """Explicit standard ports (80, 443) are on the allowlist."""
    assert web_fetch._validate_public_url("http://example.com:80/path") == "http://example.com:80/path"
    assert web_fetch._validate_public_url("https://example.com:443/path") == "https://example.com:443/path"


def test_validate_url_accepts_default_port() -> None:
    """No explicit port is allowed — the scheme default is used at connect time."""
    assert web_fetch._validate_public_url("http://example.com/path") == "http://example.com/path"


def test_validate_url_accepts_non_standard_ports() -> None:
    assert web_fetch._validate_public_url("http://localhost:8080/path") == "http://localhost:8080/path"
    assert web_fetch._validate_public_url("http://example.com:8443/path") == "http://example.com:8443/path"


def test_validate_url_rejects_malformed_port() -> None:
    """A malformed (non-numeric / out-of-range) port is rejected."""
    with pytest.raises(ValueError, match="malformed port"):
        web_fetch._validate_public_url("http://example.com:notaport/path")


# --------------------------------------------------------------------------- #
# IP validation (resolution + public-IP check)                                #
# --------------------------------------------------------------------------- #


def test_assert_fetchable_ip_rejects_loopback_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_WEB_FETCH_ALLOW_LOOPBACK", raising=False)
    for ip in ("127.0.0.1", "127.23.45.67", "::1"):
        with pytest.raises(ValueError, match="LEMONCROW_WEB_FETCH_ALLOW_LOOPBACK"):
            web_fetch._assert_fetchable_ip(ip)


def test_assert_fetchable_ip_allows_loopback_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_WEB_FETCH_ALLOW_LOOPBACK", "1")
    web_fetch._assert_fetchable_ip("127.0.0.1")
    web_fetch._assert_fetchable_ip("127.23.45.67")
    web_fetch._assert_fetchable_ip("::1")


def test_assert_fetchable_ip_rejects_private() -> None:
    with pytest.raises(ValueError, match="private/local"):
        web_fetch._assert_fetchable_ip("10.0.0.1")


def test_assert_fetchable_ip_rejects_link_local() -> None:
    with pytest.raises(ValueError, match="private/local"):
        web_fetch._assert_fetchable_ip("169.254.1.1")


def test_assert_fetchable_ip_rejects_multicast() -> None:
    with pytest.raises(ValueError, match="private/local"):
        web_fetch._assert_fetchable_ip("224.0.0.1")


def test_assert_fetchable_ip_rejects_unspecified() -> None:
    with pytest.raises(ValueError, match="private/local"):
        web_fetch._assert_fetchable_ip("0.0.0.0")


def test_assert_fetchable_ip_accepts_public_ipv4() -> None:
    web_fetch._assert_fetchable_ip("8.8.8.8")


def test_assert_fetchable_ip_accepts_public_ipv6() -> None:
    web_fetch._assert_fetchable_ip("2001:4860:4860::8888")


def test_fetch_url_allows_loopback_on_non_standard_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_WEB_FETCH_ALLOW_LOOPBACK", "1")

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"localhost fetch works"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "127.0.0.1")

    try:
        port = server.server_address[1]
        result = web_fetch.fetch_url(f"http://localhost:{port}/health", output_format="text")
        assert result["content"] == "localhost fetch works"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


# --------------------------------------------------------------------------- #
# DNS resolution with timeout                                                 #
# --------------------------------------------------------------------------- #


def test_resolve_host_safe_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    slow_future: concurrent.futures.Future = concurrent.futures.Future()

    monkeypatch.setattr(web_fetch._DNS_EXECUTOR, "submit", lambda *a, **kw: slow_future)
    monkeypatch.setattr(web_fetch._DNS_EXECUTOR, "_max_workers", 4)

    with pytest.raises(ValueError, match="timed out"):
        web_fetch._resolve_host_safe("example.com", timeout=0.05)


def test_resolve_host_safe_rejects_idn_failure() -> None:
    with pytest.raises(ValueError, match="invalid hostname"):
        web_fetch._resolve_host_safe("\ud800", timeout=5.0)


# --------------------------------------------------------------------------- #
# Content type rejection                                                      #
# --------------------------------------------------------------------------- #


def test_rejects_binary_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_http = _FakeHTTP()
    fake_http.request = lambda *a, **kw: _FakeBinaryResponse()
    monkeypatch.setattr(web_fetch, "_HTTP", fake_http)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")

    with pytest.raises(ValueError, match="unsupported content type"):
        web_fetch._fetch_uncached("https://example.com/file", accept="*/*", timeout_s=5.0)


# --------------------------------------------------------------------------- #
# PDF extraction                                                              #
# --------------------------------------------------------------------------- #


def test_accepts_pdf_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_http = _FakeHTTP()
    fake_http.request = lambda *a, **kw: _FakePdfResponse(_build_minimal_pdf())
    monkeypatch.setattr(web_fetch, "_HTTP", fake_http)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")

    raw = web_fetch._fetch_uncached("https://example.com/doc.pdf", accept="*/*", timeout_s=5.0)
    assert raw.content_type == "application/pdf"


def test_pdf_to_text_extracts_text() -> None:
    text = web_fetch._pdf_to_text(_build_minimal_pdf("Hello PDF"))
    assert text == "Hello PDF"


def test_pdf_to_text_raises_on_corrupt_pdf() -> None:
    with pytest.raises(ValueError, match="failed to extract PDF text"):
        web_fetch._pdf_to_text(b"this is not a pdf")


def test_table_to_markdown_renders_header_and_rows() -> None:
    table = [["Model", "Score"], ["Opus 4.8", "88.6"], [None, "87.6"]]
    md = web_fetch._table_to_markdown(table)
    lines = md.splitlines()
    assert lines[0] == "| Model | Score |"
    assert lines[1] == "| --- | --- |"
    assert "| Opus 4.8 | 88.6 |" in md
    assert "|  | 87.6 |" in md  # None cell renders as empty, not "None"


def test_table_to_markdown_empty_table_returns_empty_string() -> None:
    assert web_fetch._table_to_markdown([]) == ""
    assert web_fetch._table_to_markdown([[]]) == ""


class _FakePlumberPage:
    def __init__(self, text: str, tables: list[list[list[Any]]], page_number: int) -> None:
        self._text = text
        self._tables = tables
        self.page_number = page_number

    def extract_text(self) -> str:
        return self._text

    def extract_tables(self) -> list[list[list[Any]]]:
        return self._tables


class _FakePlumberPdf:
    def __init__(self, pages: list[_FakePlumberPage]) -> None:
        self.pages = pages

    def __enter__(self) -> _FakePlumberPdf:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakePypdfImage:
    def __init__(self, name: str, data: bytes) -> None:
        self.name = name
        self.data = data


class _FakePypdfPage:
    def __init__(self, images: list[_FakePypdfImage]) -> None:
        self.images = images


def test_pdf_to_text_assembles_prose_tables_and_image_notes(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Prose, detected tables (as Markdown), and per-image spill pointers must
    all survive into the extracted text -- not just the prose, which was the
    old (pypdf-text-only) behavior that silently dropped tables and images."""
    import pdfplumber
    import pypdf

    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path))

    fake_plumber_pages = [
        _FakePlumberPage("Intro prose.", [[["A", "B"], ["1", "2"]]], 1),
        _FakePlumberPage("", [], 2),
    ]
    monkeypatch.setattr(pdfplumber, "open", lambda *a, **kw: _FakePlumberPdf(fake_plumber_pages))

    fake_pypdf_pages = [
        _FakePypdfPage([]),
        _FakePypdfPage([_FakePypdfImage("Im0.png", b"fake-png-bytes"), _FakePypdfImage("Im1.jpg", b"fake-jpg-bytes")]),
    ]

    class _FakeReader:
        def __init__(self, *a: object, **kw: object) -> None:
            self.pages = fake_pypdf_pages

    monkeypatch.setattr(pypdf, "PdfReader", _FakeReader)

    text = web_fetch._pdf_to_text(b"irrelevant-bytes-since-both-parsers-are-mocked")

    assert "Intro prose." in text
    assert "| A | B |" in text
    assert "[image on page 2:" in text
    saved_images = sorted(tmp_path.glob("pdf-image-web_fetch-*"))
    assert len(saved_images) == 2
    assert {p.suffix for p in saved_images} == {".png", ".jpg"}
    assert saved_images[0].read_bytes() in (b"fake-png-bytes", b"fake-jpg-bytes")


def test_pdf_to_text_notes_images_past_the_extraction_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Past _MAX_PDF_IMAGES_EXTRACTED, remaining images are counted, not spilled."""
    import pdfplumber
    import pypdf

    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path))
    monkeypatch.setattr(web_fetch, "_MAX_PDF_IMAGES_EXTRACTED", 1)

    fake_plumber_pages = [_FakePlumberPage("Prose.", [], 1)]
    monkeypatch.setattr(pdfplumber, "open", lambda *a, **kw: _FakePlumberPdf(fake_plumber_pages))

    fake_pypdf_pages = [
        _FakePypdfPage([_FakePypdfImage("Im0.png", b"one"), _FakePypdfImage("Im1.png", b"two")]),
    ]

    class _FakeReader:
        def __init__(self, *a: object, **kw: object) -> None:
            self.pages = fake_pypdf_pages

    monkeypatch.setattr(pypdf, "PdfReader", _FakeReader)

    text = web_fetch._pdf_to_text(b"irrelevant-bytes")

    assert "[image on page 1:" in text
    assert "1 additional embedded image(s) not extracted" in text
    assert len(list(tmp_path.glob("pdf-image-web_fetch-*"))) == 1


def test_render_content_raises_clearly_on_truncated_pdf() -> None:
    """A PDF that exceeded the fetch cap must fail with an actionable message,
    not a cryptic pypdf parse error on the corrupted (truncated) bytes."""
    raw = web_fetch._RawFetchResult(
        url="https://x",
        final_url="https://x",
        status=200,
        content_type="application/pdf",
        headers={},
        body=_build_minimal_pdf("Hello PDF")[:10],  # truncated mid-file
        truncated_body=True,
    )
    with pytest.raises(ValueError, match=r"exceeds the .*MB fetch cap"):
        web_fetch._render_content(raw, requested_format="text")


def test_fetch_url_renders_pdf_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_http = _FakeHTTP()
    fake_http.request = lambda *a, **kw: _FakePdfResponse(_build_minimal_pdf("Hello PDF"))
    monkeypatch.setattr(web_fetch, "_HTTP", fake_http)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")

    result = web_fetch.fetch_url("https://example.com/doc.pdf", output_format="text")
    assert result["content"].startswith("Hello PDF")
    assert result["format"] == "text"


def test_fetch_url_pdf_points_at_the_downloaded_original(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The raw PDF is written to the spill store and the returned text names
    its path, so an agent can open the real file when extraction loses charts/tables."""
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path))
    fake_http = _FakeHTTP()
    pdf_bytes = _build_minimal_pdf("Hello PDF")
    fake_http.request = lambda *a, **kw: _FakePdfResponse(pdf_bytes)
    monkeypatch.setattr(web_fetch, "_HTTP", fake_http)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")

    result = web_fetch.fetch_url("https://example.com/doc.pdf", output_format="text")
    assert "downloaded PDF:" in result["content"]

    saved = list(tmp_path.glob("original-web_fetch-*.pdf"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == pdf_bytes


# --------------------------------------------------------------------------- #
# HTTP error codes                                                            #
# --------------------------------------------------------------------------- #


def test_returns_http_error_response_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-2xx is the origin's answer, not a tool failure -- it comes back as a
    normal result (with the real status) so the MCP layer never wraps it in a
    generic tool-call error."""
    calls = 0

    def _request(*_a: object, **_kw: object) -> Any:
        nonlocal calls
        calls += 1
        return _FakeErrorResponse()

    fake_http = _FakeHTTP()
    fake_http.request = _request
    monkeypatch.setattr(web_fetch, "_HTTP", fake_http)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")

    result = web_fetch._fetch_uncached("https://example.com/missing", accept="*/*", timeout_s=5.0)
    assert result.status == 404
    assert result.body == b"Not Found"
    # 404 is the origin's stable, permanent answer -- not in _RETRY_STATUSES, so
    # exactly one attempt.
    assert calls == 1


def test_retries_transient_403_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 403 is treated as a possible WAF/bot-management false positive: retried
    a bounded number of times before being accepted as the final answer."""
    responses = [_FakeErrorResponse(body=b"forbidden", status=403), _FakeResponse(b"# OK\n\nBody")]
    fake_http = _FakeHTTP()

    def _request(*_a: object, **_kw: object) -> Any:
        fake_http.calls += 1
        return responses.pop(0)

    fake_http.request = _request
    monkeypatch.setattr(web_fetch, "_HTTP", fake_http)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")
    monkeypatch.setattr(web_fetch, "_RETRY_BACKOFF_S", 0.0)

    result = web_fetch._fetch_uncached("https://example.com/flaky", accept="*/*", timeout_s=5.0)
    assert result.status == 200
    assert fake_http.calls == 2


def test_retries_exhaust_on_persistent_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 403 on every attempt is a real, stable block -- retried up to the
    bound, then surfaced as-is (never raised as a tool error)."""
    calls = 0

    def _request(*_a: object, **_kw: object) -> Any:
        nonlocal calls
        calls += 1
        return _FakeErrorResponse(status=403)

    fake_http = _FakeHTTP()
    fake_http.request = _request
    monkeypatch.setattr(web_fetch, "_HTTP", fake_http)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")
    monkeypatch.setattr(web_fetch, "_RETRY_BACKOFF_S", 0.0)

    result = web_fetch._fetch_uncached("https://example.com/blocked", accept="*/*", timeout_s=5.0)
    assert result.status == 403
    assert calls == web_fetch._MAX_FETCH_ATTEMPTS


def test_fetch_url_surfaces_http_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_http = _FakeHTTP()
    fake_http.request = lambda *a, **kw: _FakeErrorResponse()
    monkeypatch.setattr(web_fetch, "_HTTP", fake_http)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")

    payload = web_fetch.fetch_url("https://example.com/missing")
    assert payload["status"] == 404
    assert "Not Found" in payload["content"]


# --------------------------------------------------------------------------- #
# Redirects                                                                   #
# --------------------------------------------------------------------------- #


def test_follows_redirect_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    redirect_target = "https://example.com/final"

    class _RedirectFakeHTTP:
        def __init__(self_):
            self_.step = 0

        def request(self_, *a, **kw):
            self_.step += 1
            if self_.step == 1:
                return _FakeRedirectResponse(redirect_target)
            return _FakeResponse(b"# Final\n\nContent")

    fake = _RedirectFakeHTTP()
    monkeypatch.setattr(web_fetch, "_HTTP", fake)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")

    result = web_fetch._fetch_uncached("https://example.com/redirect", accept="*/*", timeout_s=5.0)
    assert result.final_url == redirect_target
    assert result.status == 200
    body = result.body.decode()
    assert "# Final" in body


def test_raises_on_redirect_without_location(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoLocRedirectFakeHTTP:
        def request(self_, *a, **kw):
            return _FakeRedirectResponse("")

    fake = _NoLocRedirectFakeHTTP()
    monkeypatch.setattr(web_fetch, "_HTTP", fake)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")

    with pytest.raises(ValueError, match="redirect without Location"):
        web_fetch._fetch_uncached("https://example.com/redirect-no-loc", accept="*/*", timeout_s=5.0)


# --------------------------------------------------------------------------- #
# Cache correctness (thread-safe LRU)                                         #
# --------------------------------------------------------------------------- #


def test_fetch_cache_reuses_raw_response(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_http = _FakeHTTP()
    monkeypatch.setattr(web_fetch, "_HTTP", fake_http)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")

    first = web_fetch.fetch_url("https://example.com/docs", max_chars=100)
    second = web_fetch.fetch_url("https://example.com/docs", max_chars=100)

    assert first["content"] == second["content"]
    assert fake_http.calls == 1


def test_fetch_cache_distinct_urls_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_http = _FakeHTTP()
    monkeypatch.setattr(web_fetch, "_HTTP", fake_http)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")

    web_fetch.fetch_url("https://example.com/a", max_chars=100)
    web_fetch.fetch_url("https://example.com/b", max_chars=100)

    assert fake_http.calls == 2


# --------------------------------------------------------------------------- #
# HTML / Markdown conversion                                                  #
# --------------------------------------------------------------------------- #


def test_html_to_markdown_preserves_coding_docs_structure() -> None:
    html = """
    <html><head><title>API Reference</title><script>alert(1)</script></head>
    <body><main><h1>Client</h1><p>Use <code>fetch_url</code>.</p>
    <pre class="language-python">print('ok')</pre>
    <a href="/docs/auth">Auth</a><img alt="Architecture diagram" src="/a.png" />
    </main></body></html>
    """

    markdown = web_fetch.html_to_markdown_for_agent(html, base_url="https://example.com/base")

    assert "# Client" in markdown
    assert "`fetch_url`" in markdown
    assert "```" in markdown
    assert "print('ok')" in markdown
    assert "Auth" in markdown
    assert "Architecture diagram" in markdown
    assert "alert" not in markdown


def test_html_to_markdown_handles_decomposed_descendants() -> None:
    # Regression: a hidden container holding child tags. _remove_noise
    # decomposes the container, which nulls its descendants' .attrs while those
    # descendants are still pending in find_all(True)'s materialized list.
    # Reaching one used to raise "AttributeError: 'NoneType' object has no
    # attribute 'get'" and broke every fetch of a page with such markup.
    html = (
        "<html><body>"
        '<div style="display:none"><span>secret</span><p>hidden body</p></div>'
        "<p>visible body</p>"
        "</body></html>"
    )

    markdown = web_fetch.html_to_markdown_for_agent(html)

    assert "visible body" in markdown
    assert "secret" not in markdown
    assert "hidden body" not in markdown


def test_clean_markdown_removes_converter_noise() -> None:
    cleaned = web_fetch.clean_markdown_for_agent("Title\nTitle\n\n\n\n\n![](pixel.gif)\n[](/empty)\n    ```\ncode\n```")

    assert cleaned.count("Title") == 1
    assert "pixel.gif" not in cleaned
    assert "[](" not in cleaned
    assert "```\ncode\n```" in cleaned


# --------------------------------------------------------------------------- #
# Utility helpers                                                             #
# --------------------------------------------------------------------------- #


def test_is_ip_address() -> None:
    assert web_fetch._is_ip_address("8.8.8.8") is True
    assert web_fetch._is_ip_address("::1") is True
    assert web_fetch._is_ip_address("example.com") is False
    assert web_fetch._is_ip_address("") is False


# --------------------------------------------------------------------------- #
# Query-gated relevance truncation                                            #
# --------------------------------------------------------------------------- #


def test_chunk_markdown_splits_a_big_table_one_row_per_chunk_pinned_to_header() -> None:
    md = "| Rank | Model |\n|---|---|\n" + "".join(f"| {i} | m{i} |\n" for i in range(50))
    chunks = web_fetch._chunk_markdown(md)
    assert len(chunks) == 50
    assert all(pin == "| Rank | Model |\n|---|---|" for _text, pin in chunks)


def test_chunk_markdown_keeps_short_prose_as_one_chunk() -> None:
    md = "# Title\n\nJust a short paragraph of prose."
    chunks = web_fetch._chunk_markdown(md)
    assert len(chunks) == 2
    assert all(pin is None for _text, pin in chunks)


def test_finish_fetch_without_query_is_unchanged_head_truncation() -> None:
    md = "x" * 5000
    raw = web_fetch._RawFetchResult(
        url="https://x",
        final_url="https://x",
        status=200,
        content_type="text/plain",
        headers={},
        body=md.encode(),
        truncated_body=False,
    )
    payload = web_fetch._finish_fetch(
        raw, rendered={"content": md, "format": "text"}, char_limit=1000, include_meta=False
    )
    assert payload["content"].startswith("x" * 100)
    assert "[lemon: truncated 5000→1000" in payload["content"]


def test_finish_fetch_with_query_surfaces_a_row_the_head_cut_would_miss() -> None:
    header = "| Rank | Model | Score |\n|---|---|---|\n"
    body = "".join(f"| {i} | {'Claude Code / Opus 4.6' if i == 130 else f'Model-{i}'} | {i} |\n" for i in range(142))
    md = header + body
    raw = web_fetch._RawFetchResult(
        url="https://x",
        final_url="https://x",
        status=200,
        content_type="text/html",
        headers={},
        body=md.encode(),
        truncated_body=False,
    )
    no_query = web_fetch._finish_fetch(
        raw, rendered={"content": md, "format": "markdown"}, char_limit=1500, include_meta=False
    )
    assert "Claude Code" not in no_query["content"]

    with_query = web_fetch._finish_fetch(
        raw,
        rendered={"content": md, "format": "markdown"},
        char_limit=1500,
        include_meta=False,
        query="Claude Code",
    )
    assert "Claude Code / Opus 4.6" in with_query["content"]
    assert "sections match" in with_query["content"]


# --------------------------------------------------------------------------- #
# summary=true                                                               #
# --------------------------------------------------------------------------- #

MD_PAGE = (
    "# Project Overview\n\n"
    "This project provides fast reliable order processing that integrates well.\n\n"
    "## Installation\n\n"
    "Run pip install project to get started on any platform.\n\n"
    "## Usage\n\n"
    "Call project.run() to start processing until completion.\n"
)


def _md_raw(content: str = MD_PAGE) -> web_fetch._RawFetchResult:
    return web_fetch._RawFetchResult(
        url="https://example.com",
        final_url="https://example.com",
        status=200,
        content_type="text/markdown",
        headers={},
        body=content.encode(),
        truncated_body=False,
    )


def test_finish_fetch_summary_returns_heuristic_gist_with_spill_path(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_LLM_BACKEND", raising=False)
    payload = web_fetch._finish_fetch(
        _md_raw(),
        rendered={"content": MD_PAGE, "format": "markdown"},
        char_limit=100_000,
        include_meta=False,
        summary=True,
    )
    content = payload["content"]
    assert "# Project Overview" in content
    assert "## Installation" in content
    assert "## Usage" in content
    assert "[lemon: summarized:heuristic" in content
    assert "full: read " in content
    spilled = list(tmp_path.glob("web_fetch-*.txt"))
    assert len(spilled) == 1
    assert spilled[0].read_text(encoding="utf-8") == MD_PAGE


def test_finish_fetch_summary_uses_llm_tier_when_available(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "ollama")
    monkeypatch.setenv("LEMONCROW_OLLAMA_MODEL", "qwen2.5")
    monkeypatch.setattr(internal_llm, "summarize", lambda text, **kw: "An LLM-produced gist of the page.")
    payload = web_fetch._finish_fetch(
        _md_raw(),
        rendered={"content": MD_PAGE, "format": "markdown"},
        char_limit=100_000,
        include_meta=False,
        summary=True,
    )
    content = payload["content"]
    assert "An LLM-produced gist of the page." in content
    assert "[lemon: summarized:qwen2.5" in content


def test_finish_fetch_summary_llm_failure_falls_back_silently_to_heuristic(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "ollama")

    def _boom(text: str, **kw: object) -> str:
        raise internal_llm.InternalLLMError("local model unreachable")

    monkeypatch.setattr(internal_llm, "summarize", _boom)
    payload = web_fetch._finish_fetch(
        _md_raw(),
        rendered={"content": MD_PAGE, "format": "markdown"},
        char_limit=100_000,
        include_meta=False,
        summary=True,
    )
    content = payload["content"]
    assert "[lemon: summarized:heuristic" in content
    assert "An LLM-produced gist" not in content


def test_finish_fetch_summary_spill_disabled_uses_pathless_footer(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "0")
    monkeypatch.delenv("LEMONCROW_LLM_BACKEND", raising=False)
    payload = web_fetch._finish_fetch(
        _md_raw(),
        rendered={"content": MD_PAGE, "format": "markdown"},
        char_limit=100_000,
        include_meta=False,
        summary=True,
    )
    content = payload["content"]
    assert "[lemon: truncated" in content
    assert "summarized:" not in content
    assert "read " not in content
    assert not list(tmp_path.glob("web_fetch-*.txt"))


def test_fetch_url_summary_ignored_when_query_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """summary=true + query: query is the more specific request and wins --
    served exactly as the query path would (no error, no summarized: verb)."""
    header = "| Rank | Model | Score |\n|---|---|---|\n"
    body = "".join(f"| {i} | {'Claude Code / Opus 4.6' if i == 130 else f'Model-{i}'} | {i} |\n" for i in range(142))
    md = header + body
    fake_http = _FakeHTTP()
    fake_http.request = lambda *a, **kw: _FakeResponse(md.encode())
    monkeypatch.setattr(web_fetch, "_HTTP", fake_http)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")
    result = web_fetch.fetch_url("https://example.com/table", max_chars=1500, query="Claude Code", summary=True)
    assert "Claude Code / Opus 4.6" in result["content"]
    assert "sections match" in result["content"]
    assert "summarized:" not in result["content"]


class _FakeHtmlResponse:
    status = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "text/html; charset=utf-8"}

    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, amt: int = 65536, decode_content: bool = True) -> Iterator[bytes]:
        yield self._body

    def release_conn(self) -> None:
        return None


def test_fetch_url_summary_ignored_when_type_html(monkeypatch: pytest.MonkeyPatch) -> None:
    """summary=true + type="html": type is the more specific request and wins --
    html is served exactly as if summary was never passed."""
    fake_http = _FakeHTTP()
    fake_http.request = lambda *a, **kw: _FakeHtmlResponse(b"<html><body><h1>Hi</h1></body></html>")
    monkeypatch.setattr(web_fetch, "_HTTP", fake_http)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")
    result = web_fetch.fetch_url("https://example.com/page", output_format="html", summary=True)
    assert result["format"] == "html"
    assert "summarized:" not in result["content"]
    assert "Hi" in result["content"]


def test_fetch_url_pdf_summary_flows_through_rendered_text_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """PDF pages arrive as already-rendered text by the time summary runs --
    no PDF-specific handling is needed."""
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_LLM_BACKEND", raising=False)
    fake_http = _FakeHTTP()
    fake_http.request = lambda *a, **kw: _FakePdfResponse(_build_minimal_pdf("Hello PDF"))
    monkeypatch.setattr(web_fetch, "_HTTP", fake_http)
    monkeypatch.setattr(web_fetch, "_resolve_host_safe", lambda host, timeout: "1.2.3.4")
    result = web_fetch.fetch_url("https://example.com/doc.pdf", output_format="text", summary=True)
    assert "Hello PDF" in result["content"]
    assert "[lemon: summarized:heuristic" in result["content"]
    assert "downloaded PDF:" in result["content"]


def test_collapse_image_links_and_strip_tracking() -> None:
    md = "intro ![diagram](https://cdn.example.com/a.png) then [doc](https://ex.com/p?utm_source=x&id=7)"
    out = web_fetch._strip_link_tracking(web_fetch._collapse_image_links(md))
    assert out == "intro diagram then [doc](https://ex.com/p?id=7)"


def test_strip_link_tracking_leaves_clean_urls_untouched() -> None:
    md = "[a](https://ex.com/p?id=7&page=2) and [b](https://ex.com/plain)"
    assert web_fetch._strip_link_tracking(md) == md
