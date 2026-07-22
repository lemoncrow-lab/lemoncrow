"""JSONL request/response logging for ``lc chatgpt serve`` (always on).

The operator debugging a ChatGPT connector sees only uvicorn's access lines;
this middleware captures the actual MCP traffic (JSON-RPC request + response
bodies) as one JSONL entry per request, appended to a single 0600 file the
caller (``chatgpt.py``) picks a path for — see ``default_log_dir``/
``dated_log_dir`` for the directory shape and ``ensure_log_file`` for the
eager-touch helper the CLI banner uses.

Written as a *pure ASGI middleware* rather than ``BaseHTTPMiddleware`` on
purpose: wrapping ``receive``/``send`` observes the byte stream as it flows, so
nothing is double-read, buffered, or re-framed. Response bodies are captured
uniformly (up to ``_MAX_BODY_BYTES``, same as request bodies) regardless of
content type — a one-shot SSE-framed JSON-RPC reply (POST ``/mcp`` with
``Accept: text/event-stream``, how ChatGPT calls it) is logged like any other
body. Capture is *incrementally capped*: once the cap is reached we stop
collecting but keep forwarding every chunk untouched, so even a genuinely
long-lived stream (the GET ``/mcp`` keep-alive) never grows the buffer without
bound — the log records a bounded prefix plus a ``…[truncated N bytes]`` marker.

Redaction is not optional: the ``Authorization`` header and everything through
the credential endpoints (POST ``/token``, POST ``/authorize`` — pairing code,
auth codes, PKCE verifiers, tokens) never reach the log. Logging failures are
swallowed (entry dropped) — a broken log file must never 500 the MCP flow.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.paths import default_store_root

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Cap per-body capture: an entry is a debugging aid, not an archive, and a
# runaway body must not balloon the log file (the MCP transport itself allows
# multi-MB payloads).
_MAX_BODY_BYTES = 64 * 1024

_REDACTED_BODY = "[redacted: credential endpoint]"
# POST bodies/responses on these paths carry the pairing code, authorization
# codes, PKCE verifiers, and access/refresh tokens. (/register is deliberately
# not listed: a public client_id is not a secret.)
_CREDENTIAL_PATHS = frozenset({"/token", "/authorize"})


def default_log_dir() -> Path:
    """``<store_root>/chatgpt/sessions`` — base directory the day-partitioned
    log file lives under.

    ``<store_root>`` is ``default_store_root()`` (``~/.lemoncrow``, or
    ``$LEMONCROW_ROOT`` when set) — the same root every other LemonCrow
    on-disk state lives under, with ``chatgpt/`` as this feature's peer
    subdirectory (next to ``chatgpt/oauth.json``, see
    ``mcp_oauth.default_state_path``). This used to be
    ``$XDG_STATE_HOME/lemoncrow/chatgpt_http.jsonl``; old logs at that XDG
    path are left where they are (nothing migrates them).

    This intentionally does *not* call ``paths.session_dir`` — a ChatGPT HTTP
    connector round-trip isn't a LemonCrow coding session (there is no
    host/session-id pair that function tracks for it) — it only borrows that
    function's *shape*: a dated ``YYYY/MM/DD/`` directory, not a flat file at
    the root.
    """
    return default_store_root() / "chatgpt" / "sessions"


def dated_log_dir(base: Path, *, today: date | None = None) -> Path:
    """``base/YYYY/MM/DD`` for ``today`` (local date; defaults to now).

    Public (not module-private) so the ``serve`` command can compute the
    concrete log file path once at startup without re-deriving the
    date-partitioning logic here.
    """
    d = today or date.today()
    return base / d.strftime("%Y") / d.strftime("%m") / d.strftime("%d")


def ensure_log_file(path: Path) -> Path:
    """Create (0600) ``path`` and its parent directories if missing; returns
    ``path`` unchanged. Idempotent — an existing file's contents are left
    untouched (only its mode is reasserted).

    Used by the ``serve`` banner to eagerly seed the log file *before*
    printing the live-tail hint: without this, a second terminal running the
    printed ``tail -f`` command before any request has landed gets "No such
    file or directory" and exits instead of waiting.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.chmod(path, 0o600)
    finally:
        os.close(fd)
    return path


def _client_ip(scope: Scope) -> str:
    """Real client address: first ``X-Forwarded-For`` hop (the tunnel appends
    itself), falling back to the ASGI peer (always loopback behind a tunnel)."""
    for key, value in scope.get("headers") or []:
        if key.lower() == b"x-forwarded-for":
            first = str(value.decode("latin-1")).split(",")[0].strip()
            if first:
                return first
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return ""


def _redacted_headers(headers: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
    """Request headers as a dict, with credential-bearing values masked.

    ``authorization`` keeps only the scheme (``Bearer ***``) so the log still
    shows *whether* a token was presented; ``cookie`` is masked entirely.
    """
    out: dict[str, str] = {}
    for key_bytes, value_bytes in headers:
        key = key_bytes.decode("latin-1").lower()
        value = value_bytes.decode("latin-1")
        if key == "authorization":
            scheme = value.split(" ", 1)[0] if value else ""
            out[key] = f"{scheme} ***" if scheme else "***"
        elif key == "cookie":
            out[key] = "***"
        else:
            out[key] = value
    return out


def _body_text(chunks: list[bytes], total_bytes: int | None = None) -> str:
    """Join captured chunks into loggable text, truncating at the cap.

    ``total_bytes`` is the true number of bytes seen when capture itself was
    capped incrementally (response bodies): ``chunks`` then holds only the first
    ``_MAX_BODY_BYTES`` and ``total_bytes`` drives an accurate truncation count.
    When omitted (request bodies, fully buffered) the joined length is used.
    """
    raw = b"".join(chunks)
    total = len(raw) if total_bytes is None else total_bytes
    if total > _MAX_BODY_BYTES:
        extra = total - _MAX_BODY_BYTES
        return raw[:_MAX_BODY_BYTES].decode("utf-8", errors="replace") + f"…[truncated {extra} bytes]"
    return raw.decode("utf-8", errors="replace")


class RequestLogMiddleware:
    """Append one JSONL entry per HTTP request/response to ``log_path``."""

    def __init__(self, app: ASGIApp, log_path: Path) -> None:
        self.app = app
        self.log_path = log_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        request_chunks: list[bytes] = []
        response_chunks: list[bytes] = []
        response_total = 0
        response_captured = 0
        status: int | None = None

        async def logged_receive() -> Message:
            # Captures only what the app actually reads; a request rejected
            # before its body is consumed (e.g. 401) logs an empty body.
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body") or b""
                if body:
                    request_chunks.append(bytes(body))
            return message

        async def logged_send(message: Message) -> None:
            nonlocal status, response_total, response_captured
            msg_type = message.get("type")
            if msg_type == "http.response.start":
                status = int(message.get("status") or 0)
            elif msg_type == "http.response.body":
                body = message.get("body") or b""
                if body:
                    # Count every forwarded byte (drives an accurate truncation
                    # marker) but stop *collecting* once the cap is reached, so a
                    # genuinely long-lived stream (GET /mcp keep-alive) can never
                    # grow the buffer without bound. Chunks are always forwarded
                    # untouched below.
                    response_total += len(body)
                    remaining = _MAX_BODY_BYTES - response_captured
                    if remaining > 0:
                        kept = bytes(body[:remaining])
                        response_chunks.append(kept)
                        response_captured += len(kept)
            await send(message)

        try:
            await self.app(scope, logged_receive, logged_send)
        finally:
            # Logging must never take the MCP flow down with it.
            with contextlib.suppress(Exception):
                self._write_entry(scope, status, start, request_chunks, response_chunks, response_total)

    def _write_entry(
        self,
        scope: Scope,
        status: int | None,
        start: float,
        request_chunks: list[bytes],
        response_chunks: list[bytes],
        response_total: int,
    ) -> None:
        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        if method == "POST" and path in _CREDENTIAL_PATHS:
            request_body = _REDACTED_BODY
            response_body = _REDACTED_BODY
        else:
            request_body = _body_text(request_chunks)
            response_body = _body_text(response_chunks, response_total)
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "client": _client_ip(scope),
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": round((time.perf_counter() - start) * 1000, 2),
            "request_headers": _redacted_headers(scope.get("headers") or []),
            "request_body": request_body,
            "response_body": response_body,
        }
        self._append(json.dumps(entry, ensure_ascii=False))

    def _append(self, line: str) -> None:
        """Append one line, creating the 0600 file (and parents) on first use.

        ``O_APPEND`` keeps concurrent entries whole; the explicit chmod pins
        0600 even against a permissive umask or a pre-existing looser file.
        """
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(self.log_path, 0o600)
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
