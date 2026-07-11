from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import hashlib
import io
import ipaddress
import json
import logging
import math
import os
import re
import socket
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import aiohttp
import urllib3
from aiohttp.abc import AbstractResolver, ResolveResult
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.poolmanager import SSL_KEYWORDS
from urllib3.response import BaseHTTPResponse
from urllib3.util.connection import _set_socket_options

logger = logging.getLogger(__name__)

OutputFormat = Literal["auto", "markdown", "text", "html"]

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_CHARS = 12_000
MAX_MAX_CHARS = 100_000
# Ceiling for the AUTO-sized budget (no explicit max_chars). The full page is
# always spilled on truncation, so past this the tail is cheaper to `read` from
# the spill than to keep resident; an explicit max_chars can still force up to
# MAX_MAX_CHARS.
DYNAMIC_MAX_CHARS = 40_000
MAX_BODY_BYTES = 2_000_000
# PDFs are inherently larger than HTML/text pages (embedded fonts, images) and the
# critical xref/trailer structure lives at the END of the file -- truncating at the
# generic text-page cap corrupts the file before pypdf ever gets to read it. A real
# ~100-page model system card with embedded charts runs 20MB+ (observed: 20.6MB).
# This is NOT unbounded: web_fetch runs inside a long-lived shared gateway process
# and the whole body is buffered in RAM, so one huge/malicious URL can hang or OOM
# it for every other session, not just the caller's. 50MB covers any legitimate
# paper, manual, or system card while keeping the worst-case resident buffer
# bounded. If a real PDF ever exceeds this, _render_content raises a clear "too
# large" error instead of feeding pypdf a truncated (and therefore unparseable) file.
MAX_PDF_BODY_BYTES = 50_000_000
MAX_REDIRECTS = 5
FETCH_CACHE_TTL_S = 300.0
FETCH_CACHE_MAX_ITEMS = 128
TRANSFORM_CACHE_MAX_ITEMS = 128
DNS_TIMEOUT_S = 10.0
_DNS_MAX_WORKERS = 4

# A browser-like UA + Sec-Fetch-*/Accept-Language set cuts false-positive WAF
# challenges (e.g. Cloudflare Bot Management) on sites that score bare/bot-ish
# clients harshly -- these headers don't change what content is requested,
# only how bot-like the request looks on the wire.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BROWSER_LIKE_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def _request_headers(accept: str) -> dict[str, str]:
    return {"User-Agent": DEFAULT_USER_AGENT, "Accept": accept, **_BROWSER_LIKE_HEADERS}


# A bare 403 from a WAF-fronted origin is frequently a per-request bot-score
# coin flip, not a real block -- the same request often succeeds on an
# immediate retry (observed empirically against a Cloudflare-fronted site).
# Bounded and scoped to 403 only (never other 4xx/5xx, which are the origin's
# real, stable answer) so a genuine permanent block still surfaces as-is.
# 403 covers WAF/bot-management false positives; 408/425/429/5xx cover classic
# transient-origin conditions (timeout, rate limit, momentary server error).
# Anything else (404, 401, other 4xx) is the origin's stable answer -- not retried.
_RETRY_STATUSES = frozenset({403, 408, 425, 429, 500, 502, 503, 504})
_MAX_FETCH_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.25


def _retry_should_continue(attempt: int, deadline: float, status: int | None) -> bool:
    """Shared retry-continuation policy for both the sync and async fetch loops.

    ``status`` is the HTTP status of a successful-but-retriable response, or
    ``None`` when the previous attempt raised a transport ``RuntimeError``.
    Bounding on ``deadline`` (wall-clock, not just attempt count) keeps a
    caller-supplied ``timeout_s`` an overall budget rather than a per-attempt
    one -- otherwise a persistently slow origin can block the caller for up
    to ``_MAX_FETCH_ATTEMPTS * timeout_s`` instead of roughly ``timeout_s``.
    """
    if attempt >= _MAX_FETCH_ATTEMPTS or time.monotonic() >= deadline:
        return False
    return status is None or status in _RETRY_STATUSES


_MARKDOWN_TYPES = {"text/markdown", "text/x-markdown", "text/vnd.daringfireball.markdown"}
_HTML_TYPES = {"text/html", "application/xhtml+xml"}
_TEXT_TYPES = {
    "text/plain",
    "application/json",
    "application/xml",
    "text/xml",
    *_MARKDOWN_TYPES,
    *_HTML_TYPES,
}
_PDF_TYPES = {"application/pdf"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_NON_CONTENT_HTML_RE = re.compile(
    r"<!--.*?-->|<(?:script|style|noscript|template|svg|canvas|iframe)\b[^>]*>.*?</(?:script|style|noscript|template|svg|canvas|iframe)>",
    re.IGNORECASE | re.DOTALL,
)
_NOISE_CLASS_ID_RE = re.compile(
    r"(?:^|[-_\s])(?:cookie|consent|banner|modal|newsletter|subscribe|sponsor|advertisement|ad-container|social-share|share-buttons|feedback-widget|tracking|promo)(?:$|[-_\s])",
    re.IGNORECASE,
)
_CODE_LANG_RE = re.compile(
    r"(?:^|\s)(?:language|lang|highlight-source|brush|sourceCode)[-_:]([a-zA-Z0-9_+.#-]+)(?:\s|$)",
    re.IGNORECASE,
)

_DNS_EXECUTOR: concurrent.futures.ThreadPoolExecutor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_DNS_MAX_WORKERS, thread_name_prefix="lemoncrow-dns"
)


def _resolve_host_safe(hostname: str, timeout: float) -> str:
    """Resolve *hostname* with a timeout and reject unsafe network destinations.

    Public addresses are allowed. Loopback is denied by default (localhost SSRF)
    unless ``LEMONCROW_WEB_FETCH_ALLOW_LOOPBACK=1``. Private-network, link-local,
    and otherwise non-routable addresses are always rejected.
    """
    try:
        ascii_host = hostname.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        raise ValueError(f"web_fetch invalid hostname: {hostname}") from None
    try:
        future = _DNS_EXECUTOR.submit(socket.getaddrinfo, ascii_host, None, proto=socket.IPPROTO_TCP)
        effective_timeout = min(timeout, DNS_TIMEOUT_S)
        infos = future.result(timeout=effective_timeout)
    except concurrent.futures.TimeoutError:
        raise ValueError(f"web_fetch DNS resolution timed out for: {hostname}") from None
    except OSError as exc:
        raise ValueError(f"web_fetch could not resolve host: {hostname}") from exc
    if not infos:
        raise ValueError(f"web_fetch could not resolve host: {hostname}")
    for info in infos:
        raw_ip = str(info[4][0])
        _assert_fetchable_ip(raw_ip)
    return str(infos[0][4][0])


_CGNAT_RANGE = ipaddress.ip_network("100.64.0.0/10")

_LOOPBACK_ALLOW_ENV = "LEMONCROW_WEB_FETCH_ALLOW_LOOPBACK"


def _loopback_allowed() -> bool:
    """Loopback fetches are DENIED by default (localhost SSRF); opt in via env.

    Same truthy-string convention as the other env toggles in this module
    (see ``_spill_enabled``).
    """
    return os.environ.get(_LOOPBACK_ALLOW_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _assert_fetchable_ip(raw_ip: str) -> None:
    ip = ipaddress.ip_address(raw_ip)
    if ip.is_loopback:
        if _loopback_allowed():
            return
        raise ValueError(
            f"web_fetch blocked loopback address: {raw_ip} (set {_LOOPBACK_ALLOW_ENV}=1 to allow localhost fetches)"
        )
    if ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise ValueError(f"web_fetch blocked private/local network IP: {raw_ip}")
    if ip.version == 4 and ip in _CGNAT_RANGE:
        raise ValueError(f"web_fetch blocked private/local network IP: {raw_ip}")


# --------------------------------------------------------------------------- #
# Custom urllib3 connection classes — DNS + IP validation at connect time     #
# --------------------------------------------------------------------------- #


class _ValidatingHTTPConnection(HTTPConnection):
    """HTTPConnection that resolves DNS with timeout and rejects unsafe IPs."""

    def _new_conn(self) -> socket.socket:
        host = self.host
        timeout = self.timeout if isinstance(self.timeout, (int, float)) else DNS_TIMEOUT_S
        if _is_ip_address(host):
            _assert_fetchable_ip(host)
        else:
            host = _resolve_host_safe(host, timeout=timeout)
        # Connect the socket to the validated IP via a local variable only. Do NOT
        # assign self._dns_host: urllib3 backs the self.host property (used for the
        # outgoing Host header) with _dns_host, so overwriting it with the IP would
        # send `Host: <ip>` and break name-based virtual hosting.
        conn = socket.create_connection(
            (host, self.port),
            self.timeout,
            source_address=self.source_address,
        )
        _set_socket_options(conn, self.socket_options)
        return conn


class _ValidatingHTTPSConnection(HTTPSConnection):
    """HTTPSConnection that resolves DNS with timeout and rejects unsafe IPs."""

    def _new_conn(self) -> socket.socket:
        host = self.host
        timeout = self.timeout if isinstance(self.timeout, (int, float)) else DNS_TIMEOUT_S
        if _is_ip_address(host):
            _assert_fetchable_ip(host)
        else:
            host = _resolve_host_safe(host, timeout=timeout)
        # Connect the socket to the validated IP via a local variable only. Do NOT
        # assign self._dns_host: urllib3 derives the TLS server_hostname from
        # self.host (HTTPSConnection.connect: `server_hostname = self.host`), and the
        # self.host property is backed by _dns_host. Overwriting it with the IP makes
        # TLS verify the certificate against the IP -> CERTIFICATE_VERIFY_FAILED
        # (IP address mismatch). Keep the hostname so SNI + cert matching are correct.
        conn = socket.create_connection(
            (host, self.port),
            self.timeout,
            source_address=self.source_address,
        )
        _set_socket_options(conn, self.socket_options)
        return conn


class _ValidatingHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _ValidatingHTTPConnection


class _ValidatingHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _ValidatingHTTPSConnection


class _ValidatingPoolManager(urllib3.PoolManager):
    """PoolManager that uses validating connection classes for HTTP/HTTPS."""

    def _new_pool(
        self,
        scheme: str,
        host: str,
        port: int | None,
        request_context: dict[str, Any] | None = None,
    ) -> HTTPConnectionPool | HTTPSConnectionPool:
        pool_cls: type[Any]
        if scheme == "http":
            pool_cls = _ValidatingHTTPConnectionPool
        elif scheme == "https":
            pool_cls = _ValidatingHTTPSConnectionPool
        else:
            pool_cls = self.pool_classes_by_scheme[scheme]

        pool_kwargs = (self.connection_pool_kw if request_context is None else request_context).copy()
        for key in ("scheme", "host", "port"):
            pool_kwargs.pop(key, None)
        if scheme == "http":
            for key in SSL_KEYWORDS:
                pool_kwargs.pop(key, None)
        return pool_cls(host, port, **pool_kwargs)


_HTTP = _ValidatingPoolManager(num_pools=16, maxsize=16, retries=False, cert_reqs="CERT_REQUIRED")


def _is_ip_address(value: str) -> bool:
    """Return True when *value* is a bare IP address (no DNS resolution needed)."""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class _RawFetchResult:
    url: str
    final_url: str
    status: int
    content_type: str
    headers: dict[str, str]
    body: bytes
    truncated_body: bool


@dataclass(frozen=True)
class _FetchCacheEntry:
    expires_at: float
    value: _RawFetchResult


_FETCH_CACHE: OrderedDict[tuple[str, str], _FetchCacheEntry] = OrderedDict()
_FETCH_CACHE_LOCK = Lock()
_TRANSFORM_CACHE: OrderedDict[tuple[str, str], str] = OrderedDict()
_TRANSFORM_CACHE_LOCK = Lock()


def clear_web_fetch_cache() -> None:
    """Clear in-process fetch and transform caches. Intended for tests and debugging."""
    with _FETCH_CACHE_LOCK:
        _FETCH_CACHE.clear()
    with _TRANSFORM_CACHE_LOCK:
        _TRANSFORM_CACHE.clear()


def strip_non_content_html(html: str) -> str:
    """Remove expensive, unsafe, or non-content HTML blocks before parsing."""
    if not isinstance(html, str) or not html:
        return ""
    return _NON_CONTENT_HTML_RE.sub(" ", html)


def clean_markdown_for_agent(markdown: str) -> str:
    """Conservatively normalize Markdown for coding agents without dropping content."""
    if not isinstance(markdown, str) or not markdown:
        return ""
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = text.replace("    ```", "```")
    text = re.sub(r"!\[(?:tracking|pixel|spacer|blank)?\]\([^)]+\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"!\[\]\([^)]+\)", "", text)
    text = re.sub(r"\[\]\([^)]+\)", "", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{4,}", "\n\n\n", text)

    deduped: list[str] = []
    previous: str | None = None
    for line in text.splitlines():
        key = line.strip()
        if key and key == previous:
            continue
        deduped.append(line)
        previous = key if key else None
    return "\n".join(deduped).strip()


def fetch_url(
    url: str,
    *,
    output_format: OutputFormat = "auto",
    max_chars: int | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    include_meta: bool = False,
    query: str | None = None,
    summary: bool = False,
) -> dict[str, Any]:
    """Fetch an HTTP(S) URL and return coding-agent-friendly content.

    ``max_chars`` omitted → budget auto-sizes to the rendered page size (see
    ``_dynamic_char_limit``).
    """
    requested_format = _normalize_output_format(output_format)
    char_limit = _clamp_int(max_chars, 1_000, MAX_MAX_CHARS) if max_chars is not None else None
    timeout = float(min(max(float(timeout_s), 1.0), 60.0))
    accept = _accept_header(requested_format)
    raw = _fetch_with_cache(url.strip(), accept=accept, timeout_s=timeout)
    rendered = _render_content(raw, requested_format=requested_format)
    return _finish_fetch(
        raw,
        rendered=rendered,
        char_limit=char_limit,
        include_meta=include_meta,
        query=query,
        summary=_effective_summary(summary, query, requested_format),
    )


def _spill_enabled() -> bool:
    """Mirrors the MCP dispatch layer's T7 kill switch (``LEMONCROW_TOOL_OUTPUT_SPILL``)."""
    return os.environ.get("LEMONCROW_TOOL_OUTPUT_SPILL", "1").strip().lower() in {"1", "true", "yes", "on"}


# Bounds the `summary=true` gist body -- mirrors the `read` tool's `:summary`
# budget so an agent learns one gist-size convention across both tools.
_SUMMARY_TARGET_CHARS = 4096


def _effective_summary(summary: bool, query: str | None, requested_format: str) -> bool:
    """Resolve `summary=true` against a more specific request rather than
    reject the combination: an explicit `query` (query-relevant sections win)
    or `type="html"` (raw markup isn't summarizable) each name a MORE specific
    request than a whole-page gist, so `summary` is silently dropped instead of
    raising -- the calling LLM can always summarize DOWN from the more detailed
    result for free, but recovering UP from an over-reduced one costs another
    turn. ``format`` field on the response still says exactly what was served.
    """
    return summary and not query and requested_format != "html"


def _summarize_rendered_content(content: str, *, char_limit: int) -> str:
    """``summary=true`` tier: spill the FULL rendered page (the recovery path),
    then gist it -- internal-LLM tier when configured and it succeeds
    (``summarized:{model}``), else a type-aware heuristic extractive gist
    (``summarized:heuristic``). Shares the exact ladder and verb grammar the
    `read` tool's `:summary` suffix uses (``text_summary.llm_summary_tier``) so
    an agent learns one gist convention across both tools.

    The gist body is bounded to ``min(char_limit, _SUMMARY_TARGET_CHARS)`` --
    the request's own cap still applies, but a gist should stay small even when
    ``max_chars`` was raised for a long page the caller expected to page through.
    """
    from lemoncrow.core.capabilities.tool_supervision import tool_output_spill
    from lemoncrow.core.capabilities.tool_supervision.text_summary import heuristic_summary, llm_summary_tier

    target_chars = min(char_limit, _SUMMARY_TARGET_CHARS)
    original_chars = len(content)
    record = tool_output_spill.spill(content, tool_name="web_fetch", kind="original") if _spill_enabled() else None

    tier = llm_summary_tier(content, target_chars=target_chars)
    if tier is not None:
        body, verb = tier
    else:
        body = heuristic_summary(content, target_chars=target_chars)
        verb = "summarized:heuristic"

    footer = tool_output_spill.spill_notice(
        verb=verb,
        original_chars=original_chars,
        kept_chars=len(body),
        path=record.path if record is not None else None,
    )
    return f"{body}\n\n{footer}"


def _truncate_with_spill(content: str, char_limit: int) -> str:
    """Cut *content* to ``char_limit`` without discarding the overflow.

    A page rendered well past ``char_limit`` (a long table, a big doc) used to
    have everything past the cut silently dropped -- there was no way to reach
    row 50 of a 142-row table short of raising ``max_chars`` (itself capped at
    ``MAX_MAX_CHARS``). Instead, persist the FULL rendered content to the shared
    T7 spill store (same store bash/read/code_search use) and name the path in
    the truncation notice, so ``read <path>`` (with a ``range=`` to page through
    it) recovers the rest. Falls back to a bare notice if spill is disabled or
    the write fails.
    """
    from lemoncrow.core.capabilities.tool_supervision import tool_output_spill

    head = content[:char_limit].rstrip()
    record = None
    if _spill_enabled():
        record = tool_output_spill.spill(content, tool_name="web_fetch", kind="original")
    footer = tool_output_spill.spill_notice(
        verb="truncated",
        original_chars=len(content),
        kept_chars=len(head),
        path=record.path if record is not None else None,
    )
    return f"{head}\n\n{footer}"


_TABLE_ROW_RE = re.compile(r"^\s*\|")


def _chunk_markdown(content: str) -> list[tuple[str, str | None]]:
    """Split rendered markdown into ``(text, pin)`` chunks for relevance ranking.

    Blank-line-delimited blocks are the base unit. A block that's entirely
    table rows (>=3 lines, all starting with ``|``) is split one row per
    chunk, pinned to its header+separator (the first two lines) so pulling in
    a single matched row deep in a 142-row table still shows column labels.
    An oversized non-table block is split into fixed-size line groups so one
    giant section (e.g. a long code block) can't swallow the whole budget as
    a single, unsplittable chunk.
    """
    chunks: list[tuple[str, str | None]] = []
    for block in re.split(r"\n{2,}", content):
        if not block.strip():
            continue
        lines = block.splitlines()
        non_blank = [ln for ln in lines if ln.strip()]
        if len(non_blank) >= 3 and all(_TABLE_ROW_RE.match(ln) for ln in non_blank):
            header = "\n".join(lines[:2])
            for row in lines[2:]:
                if row.strip():
                    chunks.append((row, header))
            continue
        if len(block) > 2000:
            for i in range(0, len(lines), 20):
                group = "\n".join(lines[i : i + 20])
                if group.strip():
                    chunks.append((group, None))
            continue
        chunks.append((block, None))
    return chunks


def _truncate_with_relevance(content: str, char_limit: int, query: str) -> str:
    """Query-gated alternative to the blind prefix cut.

    Ranks chunks of the page by relevance to *query* (semantic if a real
    embedder is configured, else deterministic lexical term-coverage — see
    ``tool_supervision.relevance_ranking``) and keeps the highest-scoring ones
    in original order, within ``char_limit``. The full page is still spilled
    regardless, so a bad ranking is recoverable, not a dead end: grep the
    named path for another term, or ``range=`` through it directly.
    """
    from lemoncrow.core.capabilities.tool_supervision.relevance_ranking import rank_and_select

    chunks = _chunk_markdown(content)
    if not chunks:
        return _truncate_with_spill(content, char_limit)
    assembled, meta = rank_and_select(chunks, query=query, char_budget=max(256, char_limit - 300))

    pointer = ""
    if _spill_enabled():
        from lemoncrow.core.capabilities.tool_supervision import tool_output_spill

        record = tool_output_spill.spill(content, tool_name="web_fetch", kind="original")
        if record is not None:
            pointer = f"; full {len(content)} chars: read {record.path}"
    tail = pointer if pointer else f" of {len(content)} chars"
    return f'{assembled}\n\n[{meta["chunks_kept"]}/{meta["chunks_total"]} sections match "{query}"{tail}]'


def _dynamic_char_limit(content_len: int, *, base: int = DEFAULT_MAX_CHARS) -> int:
    """Char budget scaled to the rendered page size.

    Page size is unknown until after fetch+render, so nothing is guessed up
    front. Page fits in ``base`` (``DEFAULT_MAX_CHARS``, one good article) →
    return the whole page. Bigger → budget grows with
    ``log2(content_len / base)``, capped at ``DYNAMIC_MAX_CHARS``: the full
    page is spilled on truncation, so the inline view stays bounded and the
    tail is recovered via ``read`` instead of held resident. An explicit
    ``max_chars`` still forces up to ``MAX_MAX_CHARS``.
    """
    if content_len <= base:
        return base
    return min(DYNAMIC_MAX_CHARS, int(base * (1 + math.log2(content_len / base))))


def _finish_fetch(
    raw: _RawFetchResult,
    *,
    rendered: dict[str, str],
    char_limit: int | None,
    include_meta: bool,
    query: str | None = None,
    summary: bool = False,
) -> dict[str, Any]:
    """Assemble the public fetch payload from a raw result + rendered content.

    Shared by the synchronous ``fetch_url`` and the async ``async_fetch_url`` so
    both return a byte-identical payload shape for the same inputs. ``summary``
    is the already-precedence-resolved flag (see ``_effective_summary``) -- this
    function does not re-check it against ``query``/format. ``char_limit``
    omitted by the caller → page size is known here, after fetch+render —
    apply ``_dynamic_char_limit`` instead of a flat cut.
    """
    content = rendered["content"]
    effective_limit = char_limit if char_limit is not None else _dynamic_char_limit(len(content))
    if summary:
        truncated = True
        content = _summarize_rendered_content(content, char_limit=effective_limit)
    else:
        truncated = len(content) > effective_limit
        if truncated:
            content = (
                _truncate_with_relevance(content, effective_limit, query)
                if query
                else _truncate_with_spill(content, effective_limit)
            )
    source_path = rendered.get("source_path")
    if source_path:
        content = f"{content}\n\n[downloaded PDF: {source_path}]"

    payload: dict[str, Any] = {"content": content, "format": rendered["format"]}
    if raw.status < 200 or raw.status >= 300:
        payload["status"] = raw.status
    tokens_saved = _estimate_tokens_saved(raw, content)
    if tokens_saved > 0:
        payload["tokens_saved"] = tokens_saved
    if include_meta:
        payload.update(
            {
                "url": raw.url,
                "final_url": raw.final_url,
                "content_type": raw.content_type,
                "truncated": truncated or raw.truncated_body,
                "cache_ttl_seconds": int(FETCH_CACHE_TTL_S),
            }
        )
    return payload


# --------------------------------------------------------------------------- #
# Async fetch path (Phase 3) — aiohttp with the SAME SSRF guard.              #
# A custom resolver validates every resolved IP and returns ONLY validated    #
# records, so aiohttp connects to exactly those addresses (no second          #
# resolution) — closing the DNS-rebinding TOCTOU. The original hostname is     #
# preserved for TLS SNI + certificate verification + the Host header.         #
# --------------------------------------------------------------------------- #


class _ValidatingResolver(AbstractResolver):
    """aiohttp resolver that applies web_fetch's SSRF guard at resolve time."""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        if _is_ip_address(host):
            _assert_fetchable_ip(host)
            return [
                ResolveResult(
                    hostname=host,
                    host=host,
                    port=port,
                    family=int(family),
                    proto=0,
                    flags=int(socket.AI_NUMERICHOST),
                )
            ]
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM),
                timeout=DNS_TIMEOUT_S,
            )
        except TimeoutError:
            raise ValueError(f"web_fetch DNS resolution timed out for: {host}") from None
        except OSError as exc:
            raise ValueError(f"web_fetch could not resolve host: {host}") from exc
        results: list[ResolveResult] = []
        for fam, _type, _proto, _canon, sockaddr in infos:
            ip = str(sockaddr[0])
            _assert_fetchable_ip(ip)  # raises ValueError on a blocked address
            results.append(
                ResolveResult(
                    hostname=host,
                    host=ip,
                    port=int(sockaddr[1]) if len(sockaddr) > 1 else port,
                    family=int(fam),
                    proto=0,
                    flags=int(socket.AI_NUMERICHOST),
                )
            )
        if not results:
            raise ValueError(f"web_fetch could not resolve host: {host}")
        return results

    async def close(self) -> None:
        return None


async def _async_read_limited_body(
    response: aiohttp.ClientResponse, *, max_bytes: int = MAX_BODY_BYTES
) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in response.content.iter_chunked(65_536):
        if not chunk:
            continue
        remaining = max_bytes - total
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), truncated


async def _async_fetch_uncached(url: str, *, accept: str, timeout_s: float) -> _RawFetchResult:
    """Fetch *url*, retrying transient failures (a retriable status, or a
    genuine transport error) a bounded number of times.

    A permanent failure -- SSRF block, unsupported content type, malformed
    redirect, too many redirects -- raises ``ValueError`` and is never retried.
    """
    attempt = 1
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            result = await _async_fetch_uncached_once(url, accept=accept, timeout_s=timeout_s)
        except RuntimeError:
            if not _retry_should_continue(attempt, deadline, None):
                raise
        else:
            if not _retry_should_continue(attempt, deadline, result.status):
                return result
        await asyncio.sleep(_RETRY_BACKOFF_S * attempt)
        attempt += 1


async def _async_fetch_uncached_once(url: str, *, accept: str, timeout_s: float) -> _RawFetchResult:
    current_url = _validate_public_url(url)
    headers = _request_headers(accept)
    timeout = aiohttp.ClientTimeout(connect=timeout_s, sock_connect=timeout_s, sock_read=timeout_s)
    connector = aiohttp.TCPConnector(
        resolver=_ValidatingResolver(),
        use_dns_cache=False,  # force the validating resolver on every connect
        family=socket.AF_UNSPEC,  # allow IPv4 + IPv6
        limit=8,
    )
    async with aiohttp.ClientSession(connector=connector) as session:
        for _redirect_index in range(MAX_REDIRECTS + 1):
            # aiohttp bypasses the resolver for a bare-IP host, so validate a
            # literal-IP target here (mirrors urllib3's _new_conn). Hostnames
            # are validated by _ValidatingResolver at connect time. Re-checked
            # every hop so a redirect to a private IP is caught too.
            literal_host = urlparse(current_url).hostname or ""
            if _is_ip_address(literal_host):
                _assert_fetchable_ip(literal_host)
            try:
                async with session.get(
                    current_url,
                    headers=headers,
                    timeout=timeout,
                    allow_redirects=False,
                ) as response:
                    status = int(response.status)
                    location = response.headers.get("location")
                    if status in _REDIRECT_STATUSES and location:
                        current_url = _validate_public_url(urljoin(current_url, location))
                        continue
                    if status in _REDIRECT_STATUSES:
                        raise ValueError(f"web_fetch failed: HTTP {status} redirect without Location")
                    content_type = response.headers.get("content-type", "") or ""
                    media_type = _media_type(content_type)
                    if media_type not in _TEXT_TYPES and media_type not in _PDF_TYPES:
                        raise ValueError(f"web_fetch unsupported content type: {media_type or 'unknown'}")
                    max_bytes = MAX_PDF_BODY_BYTES if media_type in _PDF_TYPES else MAX_BODY_BYTES
                    body, truncated_body = await _async_read_limited_body(response, max_bytes=max_bytes)
                    # Non-2xx is the origin's answer, not a tool failure -- surface it as a
                    # normal result (status + whatever body it sent) so the caller sees
                    # "HTTP 403" in the payload instead of a generic MCP tool-call error.
                    return _RawFetchResult(
                        url=url,
                        final_url=current_url,
                        status=status,
                        content_type=content_type,
                        headers={str(k).lower(): str(v) for k, v in response.headers.items()},
                        body=body,
                        truncated_body=truncated_body,
                    )
            except aiohttp.ClientError as exc:
                # Surface an SSRF block / resolve failure (ValueError raised by the
                # resolver) as itself; wrap genuine transport errors.
                cause = exc.__cause__ or exc.__context__
                if isinstance(cause, ValueError):
                    raise cause from None
                raise RuntimeError(f"web_fetch failed: {exc}") from exc
    raise ValueError("web_fetch failed: too many redirects")


async def _async_fetch_with_cache(url: str, *, accept: str, timeout_s: float) -> _RawFetchResult:
    cache_key = (url, accept)
    now = time.monotonic()
    with _FETCH_CACHE_LOCK:
        cached = _FETCH_CACHE.get(cache_key)
        if cached is not None and cached.expires_at > now:
            _FETCH_CACHE.move_to_end(cache_key)
            return cached.value
        if cached is not None:
            _FETCH_CACHE.pop(cache_key, None)

    result = await _async_fetch_uncached(url, accept=accept, timeout_s=timeout_s)
    with _FETCH_CACHE_LOCK:
        _FETCH_CACHE[cache_key] = _FetchCacheEntry(expires_at=now + FETCH_CACHE_TTL_S, value=result)
        _FETCH_CACHE.move_to_end(cache_key)
        while len(_FETCH_CACHE) > FETCH_CACHE_MAX_ITEMS:
            _FETCH_CACHE.popitem(last=False)
    return result


async def async_fetch_url(
    url: str,
    *,
    output_format: OutputFormat = "auto",
    max_chars: int | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    include_meta: bool = False,
    query: str | None = None,
    summary: bool = False,
) -> dict[str, Any]:
    """Async twin of :func:`fetch_url` — identical SSRF guard and output shape.

    Network I/O runs on the caller's event loop; the CPU-heavy HTML->Markdown
    render is offloaded to the default executor so it never blocks the loop.
    ``max_chars`` omitted → budget auto-sizes to the rendered page size (see
    ``_dynamic_char_limit``).
    """
    requested_format = _normalize_output_format(output_format)
    char_limit = _clamp_int(max_chars, 1_000, MAX_MAX_CHARS) if max_chars is not None else None
    timeout = float(min(max(float(timeout_s), 1.0), 60.0))
    accept = _accept_header(requested_format)
    raw = await _async_fetch_with_cache(url.strip(), accept=accept, timeout_s=timeout)
    loop = asyncio.get_running_loop()
    rendered = await loop.run_in_executor(
        None, functools.partial(_render_content, raw, requested_format=requested_format)
    )
    return _finish_fetch(
        raw,
        rendered=rendered,
        char_limit=char_limit,
        include_meta=include_meta,
        query=query,
        summary=_effective_summary(summary, query, requested_format),
    )


def _normalize_output_format(output_format: str) -> OutputFormat:
    normalized = str(output_format or "auto").strip().lower()
    if normalized not in {"auto", "markdown", "text", "html"}:
        raise ValueError("output_format must be one of: auto, markdown, text, html")
    return normalized  # type: ignore[return-value]


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        coerced = DEFAULT_MAX_CHARS
    return max(minimum, min(maximum, coerced))


def _accept_header(output_format: OutputFormat) -> str:
    if output_format == "html":
        return "text/html, application/xhtml+xml;q=0.9, text/markdown;q=0.5, text/plain;q=0.4, */*;q=0.1"
    if output_format == "text":
        return "text/markdown, text/plain;q=0.9, text/html;q=0.8, application/json;q=0.6, */*;q=0.1"
    return "text/markdown, text/html;q=0.9, application/xhtml+xml;q=0.8, text/plain;q=0.7, application/json;q=0.6, */*;q=0.1"


def _fetch_with_cache(url: str, *, accept: str, timeout_s: float) -> _RawFetchResult:
    cache_key = (url, accept)
    now = time.monotonic()
    with _FETCH_CACHE_LOCK:
        cached = _FETCH_CACHE.get(cache_key)
        if cached is not None and cached.expires_at > now:
            _FETCH_CACHE.move_to_end(cache_key)
            return cached.value
        if cached is not None:
            _FETCH_CACHE.pop(cache_key, None)

    result = _fetch_uncached(url, accept=accept, timeout_s=timeout_s)
    with _FETCH_CACHE_LOCK:
        _FETCH_CACHE[cache_key] = _FetchCacheEntry(expires_at=now + FETCH_CACHE_TTL_S, value=result)
        _FETCH_CACHE.move_to_end(cache_key)
        while len(_FETCH_CACHE) > FETCH_CACHE_MAX_ITEMS:
            _FETCH_CACHE.popitem(last=False)
    return result


def _fetch_uncached(url: str, *, accept: str, timeout_s: float) -> _RawFetchResult:
    """Fetch *url*, retrying transient failures (a retriable status, or a
    genuine transport error) a bounded number of times.

    A permanent failure -- SSRF block, unsupported content type, malformed
    redirect, too many redirects -- raises ``ValueError`` and is never retried.
    """
    attempt = 1
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            result = _fetch_uncached_once(url, accept=accept, timeout_s=timeout_s)
        except RuntimeError:
            if not _retry_should_continue(attempt, deadline, None):
                raise
        else:
            if not _retry_should_continue(attempt, deadline, result.status):
                return result
        time.sleep(_RETRY_BACKOFF_S * attempt)
        attempt += 1


def _fetch_uncached_once(url: str, *, accept: str, timeout_s: float) -> _RawFetchResult:
    current_url = _validate_public_url(url)
    headers = _request_headers(accept)
    timeout = urllib3.Timeout(connect=timeout_s, read=timeout_s)

    for _redirect_index in range(MAX_REDIRECTS + 1):
        try:
            response = _HTTP.request(
                "GET",
                current_url,
                headers=headers,
                timeout=timeout,
                preload_content=False,
                redirect=False,
            )
        except (urllib3.exceptions.HTTPError, ValueError) as exc:
            # Surface an SSRF block / resolve failure (ValueError raised by the
            # connection guard) as itself; wrap genuine transport errors.
            cause = exc if isinstance(exc, ValueError) else (exc.__cause__ or exc.__context__)
            if isinstance(cause, ValueError):
                raise cause from None
            raise RuntimeError(f"web_fetch failed: {exc}") from exc

        try:
            status = int(response.status)
            location = response.headers.get("location")
            if status in _REDIRECT_STATUSES and location:
                response.release_conn()
                current_url = _validate_public_url(urljoin(current_url, location))
                continue
            if status in _REDIRECT_STATUSES:
                raise ValueError(f"web_fetch failed: HTTP {status} redirect without Location")
            content_type = response.headers.get("content-type", "") or ""
            media_type = _media_type(content_type)
            if media_type not in _TEXT_TYPES and media_type not in _PDF_TYPES:
                raise ValueError(f"web_fetch unsupported content type: {media_type or 'unknown'}")
            max_bytes = MAX_PDF_BODY_BYTES if media_type in _PDF_TYPES else MAX_BODY_BYTES
            body, truncated_body = _read_limited_body(response, max_bytes=max_bytes)
            # Non-2xx is the origin's answer, not a tool failure -- surface it as a
            # normal result (status + whatever body it sent) so the caller sees
            # "HTTP 403" in the payload instead of a generic MCP tool-call error.
            return _RawFetchResult(
                url=url,
                final_url=current_url,
                status=status,
                content_type=content_type,
                headers={str(k).lower(): str(v) for k, v in response.headers.items()},
                body=body,
                truncated_body=truncated_body,
            )
        finally:
            response.release_conn()
    raise ValueError("web_fetch failed: too many redirects")


def _read_limited_body(response: BaseHTTPResponse, *, max_bytes: int = MAX_BODY_BYTES) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    for chunk in response.stream(amt=65_536, decode_content=True):
        if not chunk:
            continue
        remaining = max_bytes - total
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), truncated


def _validate_public_url(url: str) -> str:
    """Basic URL format validation — DNS + IP check happens at connect time."""
    if not url or _CONTROL_CHARS_RE.search(url):
        raise ValueError("web_fetch URL is empty or contains control characters")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("web_fetch only supports http and https URLs")
    if not parsed.hostname:
        raise ValueError("web_fetch URL must include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("web_fetch does not allow embedded credentials")
    try:
        _ = parsed.port
    except ValueError:
        raise ValueError("web_fetch URL has a malformed port") from None
    return url


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _decode_body(body: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([^;]+)", content_type, flags=re.IGNORECASE)
    encoding = charset_match.group(1).strip().strip('"') if charset_match else "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _render_content(raw: _RawFetchResult, *, requested_format: OutputFormat) -> dict[str, str]:
    media_type = _media_type(raw.content_type)
    if media_type in _PDF_TYPES:
        if raw.truncated_body:
            cap_mb = MAX_PDF_BODY_BYTES // 1_000_000
            raise ValueError(
                f"web_fetch: PDF exceeds the {cap_mb}MB fetch cap and was truncated mid-download; "
                "a truncated PDF has no valid trailer and can't be parsed. Fetch a smaller "
                "page range, an HTML/text version of the same document, or ask for a raise to "
                "MAX_PDF_BODY_BYTES if this document is legitimately larger."
            )
        # Binary format -- must not go through _decode_body's text charset decode.
        rendered: dict[str, str] = {"content": _pdf_to_text(raw.body), "format": "text"}
        pdf_record = _spill_original_pdf(raw.body)
        if pdf_record is not None:
            rendered["source_path"] = str(pdf_record.path)
        return rendered
    decoded = _decode_body(raw.body, raw.content_type)
    if media_type in _MARKDOWN_TYPES:
        markdown = clean_markdown_for_agent(decoded)
        return _format_markdown(markdown, requested_format=requested_format)
    if media_type in _HTML_TYPES:
        if requested_format == "html":
            return {"content": _sanitize_html(decoded, base_url=raw.final_url), "format": "html"}
        markdown = _trafilatura_markdown(decoded, base_url=raw.final_url)
        if _markdown_looks_weak(markdown, decoded):
            markdown = html_to_markdown_for_agent(decoded, base_url=raw.final_url)
        return _format_markdown(
            _strip_link_tracking(_collapse_image_links(markdown)), requested_format=requested_format
        )
    if media_type == "application/json":
        return {"content": _format_json(decoded), "format": "text" if requested_format == "text" else "markdown"}
    text = _normalize_plain_text(decoded)
    return {"content": text, "format": "text"}


def _format_markdown(markdown: str, *, requested_format: OutputFormat) -> dict[str, str]:
    if requested_format == "text":
        return {"content": _markdown_to_plain_text(markdown), "format": "text"}
    if requested_format == "html":
        return {"content": markdown, "format": "markdown"}
    return {"content": markdown, "format": "markdown"}


def html_to_markdown_for_agent(html: str, *, base_url: str = "") -> str:
    """Convert HTML to Markdown while preserving coding-doc structure."""
    cache_key = _transform_cache_key("html_markdown", html + "\0" + base_url)
    cached = _get_transform_cache(cache_key)
    if cached is not None:
        return cached
    result = _html_to_markdown_uncached(html, base_url=base_url)
    _set_transform_cache(cache_key, result)
    return result


def _html_to_markdown_uncached(html: str, *, base_url: str) -> str:
    sanitized_html = strip_non_content_html(html) or html
    soup = _soup(sanitized_html)
    _remove_noise(soup)
    _normalize_links_and_images(soup, base_url=base_url)
    root = _select_content_root(soup)
    source = str(root) if root is not None else str(soup)
    markdown = _markdownify_html(source)
    prefix = _small_metadata_prefix(soup, markdown)
    return clean_markdown_for_agent(prefix + markdown)


def _soup(html: str) -> Any:
    from bs4 import BeautifulSoup, FeatureNotFound

    try:
        return BeautifulSoup(html, "lxml")
    except (AttributeError, TypeError, ValueError, FeatureNotFound):
        return BeautifulSoup(html, "html.parser")


def _remove_noise(soup: Any) -> None:
    for tag in soup.find_all(
        [
            "script",
            "style",
            "noscript",
            "template",
            "svg",
            "canvas",
            "iframe",
            "form",
            "input",
            "button",
            "select",
            "textarea",
        ]
    ):
        tag.decompose()
    for tag in soup.find_all(True):
        # decompose() below also tears down a tag's descendants (setting their
        # .attrs to None), but find_all(True) already materialized those
        # descendants into this list. Skip any a prior iteration decomposed --
        # Tag.get() on a None .attrs raises AttributeError.
        if tag.attrs is None:
            continue
        style = str(tag.get("style") or "").lower()
        if (
            tag.has_attr("hidden")
            or tag.get("aria-hidden") == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        ):
            tag.decompose()
            continue
        marker = " ".join([str(tag.get("id") or ""), " ".join(str(c) for c in tag.get("class") or [])])
        if _NOISE_CLASS_ID_RE.search(marker):
            tag.decompose()


def _normalize_links_and_images(soup: Any, *, base_url: str) -> None:
    from bs4.element import NavigableString

    for tag in soup.find_all("a"):
        href = str(tag.get("href") or "").strip()
        if href and not href.startswith(("#", "mailto:", "tel:")):
            tag["href"] = urljoin(base_url, href)
    for tag in soup.find_all("img"):
        alt = str(tag.get("alt") or "").strip()
        if alt:
            tag.replace_with(NavigableString(alt))
        else:
            tag.decompose()


def _select_content_root(soup: Any) -> Any:
    selectors = [
        "article",
        "main",
        "[role=main]",
        ".markdown-body",
        ".theme-doc-markdown",
        ".docs-content",
        ".documentation",
        "#content",
        "#main-content",
        "body",
    ]
    candidates: list[Any] = []
    for selector in selectors:
        candidates.extend(soup.select(selector))
    if not candidates:
        return soup.body or soup
    return max(candidates, key=_content_score)


def _content_score(node: Any) -> int:
    text = node.get_text(" ", strip=True)
    score = len(text)
    score += 400 * len(node.find_all(["pre", "code"]))
    score += 150 * len(node.find_all(["h1", "h2", "h3"]))
    score += 80 * len(node.find_all("table"))
    return score


def _markdownify_html(html: str) -> str:
    import markdownify

    try:
        return str(
            markdownify.markdownify(
                html,
                heading_style="ATX",
                bullets="-",
                strip=[
                    "script",
                    "style",
                    "noscript",
                    "template",
                    "svg",
                    "canvas",
                    "iframe",
                    "form",
                    "input",
                    "button",
                    "select",
                    "textarea",
                ],
                code_language_callback=_code_language_callback,
                table_infer_header=True,
                wrap=False,
                autolinks=False,
                default_title=False,
            )
        )
    except TypeError:
        return str(markdownify.markdownify(html, heading_style="ATX", bullets="-", strip=["script", "style"]))


def _code_language_callback(el: Any) -> str | None:
    attrs = [str(el.get("class") or ""), str(el.get("data-lang") or ""), str(el.get("data-language") or "")]
    parent = getattr(el, "parent", None)
    if parent is not None:
        attrs.append(str(parent.get("class") or ""))
    joined = " ".join(attrs)
    match = _CODE_LANG_RE.search(joined)
    return match.group(1).lower() if match else None


def _small_metadata_prefix(soup: Any, markdown: str) -> str:
    parts: list[str] = []
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if title and not markdown.lstrip().startswith("#"):
        parts.append(f"# {title}")
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = str(meta.get("content") or "").strip() if meta else ""
    if description and description not in markdown and len(description) <= 300:
        parts.append(description)
    return ("\n\n".join(parts) + "\n\n") if parts else ""


def _sanitize_html(html: str, *, base_url: str) -> str:
    soup = _soup(strip_non_content_html(html) or html)
    _remove_noise(soup)
    _normalize_links_and_images(soup, base_url=base_url)
    root = _select_content_root(soup)
    return str(root or soup).strip()


def _trafilatura_markdown(html: str, *, base_url: str) -> str:
    import trafilatura

    try:
        extracted = trafilatura.extract(
            html,
            url=base_url or None,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_links=True,
            deduplicate=True,
            favor_precision=False,
            favor_recall=True,
        )
    except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
        return ""
    return clean_markdown_for_agent(extracted or "")


def _markdown_looks_weak(markdown: str, html: str) -> bool:
    if len(html) < 5_000:
        return False
    if len(markdown.strip()) < 300:
        return True
    code_or_table = markdown.count("```") + markdown.count("|")
    return code_or_table == 0 and len(markdown) < len(html) * 0.03


def _spill_original_pdf(body: bytes) -> Any:
    """Persist the raw downloaded PDF bytes so an agent can open the real file.

    Extraction is text-only and loses charts/scanned pages/complex layout;
    stashing the original next to the extracted text gives a fallback the
    agent (or a human) can open directly. Gated by the same T7 spill kill
    switch as text overflow spilling. Best-effort: returns ``None`` on any
    failure so a PDF still renders even if the spill write doesn't happen.
    """
    if not _spill_enabled():
        return None
    from lemoncrow.core.capabilities.tool_supervision import tool_output_spill

    return tool_output_spill.spill_bytes(body, tool_name="web_fetch", kind="original", suffix=".pdf")


def _table_to_markdown(table: list[list[Any]]) -> str:
    """Render a pdfplumber-extracted table (rows of cells) as a Markdown table."""
    if not table or not any(table):
        return ""
    rows = [[("" if cell is None else str(cell).strip().replace("\n", " ")) for cell in row] for row in table]
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header, *body = rows
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return "\n".join(lines)


# Hard cap on embedded images extracted to individual files per PDF. Some decks
# embed hundreds of tiny decorative images; without a cap a single pathological
# document could spill hundreds of files and dominate fetch latency.
_MAX_PDF_IMAGES_EXTRACTED = 200
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif"}


def _spill_pdf_image(data: bytes, *, suffix: str) -> Any:
    """Persist one embedded PDF image and return its spill record (or None)."""
    if not _spill_enabled():
        return None
    from lemoncrow.core.capabilities.tool_supervision import tool_output_spill

    normalized_suffix = suffix if suffix.lower() in _IMAGE_SUFFIXES else ".bin"
    return tool_output_spill.spill_bytes(data, tool_name="web_fetch", kind="pdf-image", suffix=normalized_suffix)


def _pdf_to_text(body: bytes) -> str:
    """Extract a PDF's content as text, preserving as much information as possible.

    Uses pdfplumber (layout-aware, built on pdfminer.six) for prose + tables --
    naive stream-order extraction jumbles multi-column pages and completely
    discards table structure. Detected tables render as Markdown tables (may
    duplicate a table's cell text that also appears jumbled in the prose --
    overinclusion is the right default when the goal is not losing information).

    Embedded images/figures are extracted via pypdf (its image reconstruction
    handles more embedded encodings than pdfplumber exposes) and each spilled
    to its OWN small file, with an inline pointer at the point in the text
    where the image appeared -- so an agent that needs one chart can open that
    one file instead of the whole PDF. Capped at ``_MAX_PDF_IMAGES_EXTRACTED``;
    remaining images past the cap are just counted.

    Lazily imported like the other transform libraries in this module
    (markdownify, trafilatura, bs4) so the dependency cost is only paid when a
    PDF is actually fetched.
    """
    import pdfplumber
    from pypdf import PdfReader

    images_extracted = 0
    images_capped = 0

    def _image_notes(images: list[Any], page_number: int) -> str:
        nonlocal images_extracted, images_capped
        notes: list[str] = []
        for image in images:
            if images_extracted >= _MAX_PDF_IMAGES_EXTRACTED:
                images_capped += 1
                continue
            try:
                suffix = Path(str(image.name)).suffix
                record = _spill_pdf_image(image.data, suffix=suffix)
            except (OSError, ValueError, AttributeError):
                record = None
            if record is not None:
                notes.append(f"[image on page {page_number}: {record.path}]")
                images_extracted += 1
            else:
                notes.append(f"[page {page_number} has an embedded image not represented as text]")
        return "\n".join(notes)

    try:
        pages_out: list[str] = []
        try:
            pypdf_pages = list(PdfReader(io.BytesIO(body)).pages)
        except (OSError, ValueError):  # image extraction is a bonus -- text/tables must not fail because of it
            pypdf_pages = []
        with pdfplumber.open(io.BytesIO(body)) as pdf:
            for i, page in enumerate(pdf.pages):
                parts: list[str] = []
                text = (page.extract_text() or "").strip()
                if text:
                    parts.append(text)
                for table in page.extract_tables():
                    markdown_table = _table_to_markdown(table)
                    if markdown_table:
                        parts.append(markdown_table)
                try:
                    page_images = pypdf_pages[i].images if i < len(pypdf_pages) else []
                except (IndexError, AttributeError):
                    page_images = []
                if page_images:
                    note = _image_notes(list(page_images), page.page_number)
                    if note:
                        parts.append(note)
                if parts:
                    pages_out.append("\n\n".join(parts))
    except Exception as exc:  # pdfplumber/pdfminer raise a variety of error types on malformed input
        raise ValueError(f"web_fetch: failed to extract PDF text: {exc}") from exc
    text = "\n\n".join(page for page in pages_out if page.strip())
    if images_capped:
        text += (
            f"\n\n[{images_capped} additional embedded image(s) not extracted "
            f"-- image cap ({_MAX_PDF_IMAGES_EXTRACTED}) reached; see the raw PDF]"
        )
    if not text:
        raise ValueError("web_fetch: PDF contains no extractable text (likely scanned/image-only)")
    return _normalize_plain_text(text)


def _format_json(text: str) -> str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return _normalize_plain_text(text)
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def _normalize_plain_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


# Inline images on converted HTML pages: the target URL is bytes the model can
# neither render nor usefully dereference -- keep the alt text, drop the link.
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")


def _collapse_image_links(markdown: str) -> str:
    return _MD_IMAGE_RE.sub(lambda m: m.group(1), markdown)


# Tracking query params carried by inline links on converted HTML pages: pure
# context weight (long opaque hashes the agent never dereferences). Only these
# known keys are dropped -- every other query param is information and stays.
_TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid"})
_MD_INLINE_LINK_RE = re.compile(r"(\[[^\]]*\])\((https?://[^()\s]+)\)")


def _strip_link_tracking(markdown: str) -> str:
    """Drop known tracking params (utm_*, fbclid, gclid, ...) from inline links."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    def _clean(match: re.Match[str]) -> str:
        url = match.group(2)
        try:
            parts = urlsplit(url)
        except ValueError:
            return match.group(0)
        if not parts.query:
            return match.group(0)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        kept = [(k, v) for k, v in pairs if not (k.lower().startswith("utm_") or k.lower() in _TRACKING_QUERY_KEYS)]
        if len(kept) == len(pairs):
            return match.group(0)
        cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))
        return f"{match.group(1)}({cleaned})"

    return _MD_INLINE_LINK_RE.sub(_clean, markdown)


def _markdown_to_plain_text(markdown: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", markdown)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[`*_~>]", "", text)
    return _normalize_plain_text(text)


def _estimate_tokens_saved(raw: _RawFetchResult, content: str) -> int:
    media_type = _media_type(raw.content_type)
    if media_type not in _HTML_TYPES:
        return 0
    raw_tokens = max(0, len(raw.body.decode("utf-8", errors="ignore")) // 4)
    rendered_tokens = max(0, len(content) // 4)
    return max(0, raw_tokens - rendered_tokens)


def _transform_cache_key(name: str, content: str) -> tuple[str, str]:
    digest = hashlib.blake2b(content.encode("utf-8", errors="ignore"), digest_size=16).hexdigest()
    return name, digest


def _get_transform_cache(cache_key: tuple[str, str]) -> str | None:
    with _TRANSFORM_CACHE_LOCK:
        cached = _TRANSFORM_CACHE.get(cache_key)
        if cached is not None:
            _TRANSFORM_CACHE.move_to_end(cache_key)
        return cached


def _set_transform_cache(cache_key: tuple[str, str], value: str) -> None:
    with _TRANSFORM_CACHE_LOCK:
        _TRANSFORM_CACHE[cache_key] = value
        _TRANSFORM_CACHE.move_to_end(cache_key)
        while len(_TRANSFORM_CACHE) > TRANSFORM_CACHE_MAX_ITEMS:
            _TRANSFORM_CACHE.popitem(last=False)
