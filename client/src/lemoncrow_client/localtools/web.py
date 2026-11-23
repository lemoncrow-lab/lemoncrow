"""Public-page fetching on the host, independent of the repository server."""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import time
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

from .. import webtransport
from . import LocalContext, LocalResult, text_result
from .shell import spill_text


class _Markdown(HTMLParser):
    def __init__(self, url: str, *, plain: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.url = url
        self.plain = plain
        self.parts: list[str] = []
        self.skip = 0
        self.pre = False
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "footer"}:
            self.skip += 1
        if self.skip:
            return
        attr = dict(attrs)
        if tag in {"p", "div", "section", "article", "blockquote", "table", "tr"}:
            self.parts.append("\n\n")
        elif tag == "br":
            self.parts.append("\n")
        elif re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n" + ("" if self.plain else "#" * int(tag[1]) + " "))
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "pre":
            self.pre = True
            self.parts.append("\n\n" + ("" if self.plain else "```\n"))
        elif tag == "code" and not self.pre and not self.plain:
            self.parts.append("`")
        elif tag in {"strong", "b"} and not self.plain:
            self.parts.append("**")
        elif tag in {"em", "i"} and not self.plain:
            self.parts.append("*")
        elif tag == "a":
            self.links.append(urljoin(self.url, attr.get("href") or ""))
            if not self.plain:
                self.parts.append("[")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "footer"}:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in {"p", "div", "section", "article", "blockquote", "tr"} or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n")
        elif tag == "pre":
            self.parts.append(("\n```" if not self.plain else "") + "\n\n")
            self.pre = False
        elif tag == "code" and not self.pre and not self.plain:
            self.parts.append("`")
        elif tag in {"strong", "b"} and not self.plain:
            self.parts.append("**")
        elif tag in {"em", "i"} and not self.plain:
            self.parts.append("*")
        elif tag == "a" and self.links:
            href = self.links.pop()
            if not self.plain:
                self.parts.append("](" + href + ")")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data if self.pre else re.sub(r"\s+", " ", data))

    def result(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()


def _render(page: webtransport.Page, kind: str) -> tuple[str, str]:
    media = page.content_type.split(";", 1)[0].strip().lower()
    if media == "application/pdf":
        binary = shutil.which("pdftotext")
        if binary is None:
            raise ValueError("PDF extraction requires pdftotext on the client PATH")
        result = subprocess.run([binary, "-layout", "-", "-"], input=page.body, capture_output=True, timeout=20)
        if result.returncode:
            raise ValueError("web_fetch could not extract PDF text")
        return result.stdout.decode("utf-8", errors="replace").strip(), "text"
    charset = re.search(r"charset=[\"']?([\w-]+)", page.content_type, re.I)
    content = page.body.decode(charset.group(1) if charset else "utf-8", errors="replace")
    if media in {"text/html", "application/xhtml+xml"} and kind != "html":
        parser = _Markdown(page.url, plain=kind == "text")
        parser.feed(content)
        return parser.result(), "text" if kind == "text" else "markdown"
    return content.strip(), (
        "html" if media in {"text/html", "application/xhtml+xml"} else ("markdown" if "markdown" in media else "text")
    )


def _select(content: str, limit: int, query: str) -> str:
    terms = set(re.findall(r"\w+", query.lower()))
    chunks = re.split(r"\n\s*\n", content)
    scores = [len(terms & set(re.findall(r"\w+", chunk.lower()))) for chunk in chunks]
    selected: set[int] = set()
    used = 0
    for index in sorted(range(len(chunks)), key=lambda i: (-scores[i], i)):
        if used >= limit:
            break
        selected.add(index)
        used += len(chunks[index]) + 2
    return "\n\n".join(chunks[i] for i in sorted(selected))[:limit]


def run_web_fetch(context: LocalContext, arguments: Mapping[str, Any]) -> LocalResult:
    url = str(arguments.get("url") or "").strip()
    kind = str(arguments.get("type") or "auto").lower()
    if kind not in {"auto", "markdown", "text", "html"}:
        return text_result("type must be auto, markdown, text or html", is_error=True)
    accept = "text/html" if kind == "html" else "text/markdown, text/html;q=0.9, text/plain;q=0.8, */*;q=0.1"
    try:
        webtransport.validate_url(url)
        key = (url, accept)
        now = time.monotonic()
        with context.memory.web_fetch_lock:
            cache = context.memory.web_fetch_cache
            for expired in [k for k, (until, _) in cache.items() if until <= now]:
                del cache[expired]
            cached = cache.get(key)
        if cached is None:
            page = webtransport.fetch_page(
                url,
                accept=accept,
                allow_loopback=context.environment.get("LEMONCROW_WEB_FETCH_ALLOW_LOOPBACK", "").lower()
                in {"1", "true", "yes", "on"},
            )
            # At most 16 small pages (32 MB); PDFs are never retained.
            if (
                page.status == 200
                and len(page.body) <= webtransport.MAX_BODY_BYTES
                and page.content_type.split(";", 1)[0].strip().lower() != "application/pdf"
            ):
                with context.memory.web_fetch_lock:
                    cache.pop(key, None)
                    cache[key] = (time.monotonic() + 300, page)
                    while len(cache) > 16:
                        del cache[next(iter(cache))]
        else:
            page = cached[1]
        content, output_format = _render(page, kind)
    except (ValueError, LookupError, OSError, webtransport.HTTPException, subprocess.TimeoutExpired) as exc:
        return text_result(f"web_fetch failed: {exc}", is_error=True)
    limit = min(40_000, int(12_000 * (1 + math.log2(max(1, len(content) / 12_000)))))
    query = str(arguments.get("query") or "")
    summary = bool(arguments.get("summary")) and not query and kind != "html"
    if summary:
        limit = min(limit, 4096)
    if len(content) > limit:
        path = spill_text(content, kind="web-fetch", suffix=".md")
        total = len(content)
        content = _select(content, limit, query) if query else content[:limit]
        content += f"\n\n[{'summarized:heuristic' if summary else 'truncated'}: {total} chars"
        content += f"; full page: read {path}]" if path else "]"
    payload: dict[str, Any] = {"content": content, "format": output_format}
    saved = 0
    if page.content_type.split(";", 1)[0].strip().lower() in {"text/html", "application/xhtml+xml"}:
        # The full server's credit: the page's HTML against what the model got.
        saved = max(0, len(page.body.decode("utf-8", errors="ignore")) // 4 - len(content) // 4)
    if not 200 <= page.status < 300:
        payload["status"] = page.status
        content = f"HTTP {page.status}\n\n{content}"
    return text_result(content, structured=payload, tokens_saved=saved)
