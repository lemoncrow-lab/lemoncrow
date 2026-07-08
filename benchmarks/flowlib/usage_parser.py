"""Parse token usage from captured Claude Code <-> provider HTTP responses.

Pure functions, no mitmproxy dependency, so they unit-test with synthetic
bytes. ``report.py`` feeds these the ``(content_type, body)`` pairs it reads
out of a mitmproxy ``.flow`` capture.

Three response encodings are handled, covering the paths Claude Code uses:

* ``application/vnd.amazon.eventstream`` -- Amazon Bedrock streaming
  (``invoke-with-response-stream``). AWS event-stream binary framing wraps
  base64-encoded Anthropic event JSON.
* ``text/event-stream`` -- Anthropic-direct streaming (SSE).
* ``application/json`` -- non-streaming responses (either provider).

Usage fields are read tolerantly across snake_case (Anthropic) and camelCase
(Bedrock Converse) spellings. ``input_tokens`` is always the *non-cached*
prompt count, matching the provider convention; total prompt tokens are
``input + cache_read + cache_write``.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass

_INPUT_KEYS = ("input_tokens", "inputTokens")
_OUTPUT_KEYS = ("output_tokens", "outputTokens")
_CACHE_READ_KEYS = (
    "cache_read_input_tokens",
    "cacheReadInputTokens",
    "cacheReadInputTokenCount",
)
_CACHE_WRITE_KEYS = (
    "cache_creation_input_tokens",
    "cacheWriteInputTokens",
    "cacheWriteInputTokenCount",
)


@dataclass
class Usage:
    """Token usage for one request/response (or an accumulated run)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    # 1h-TTL subset of cache_creation_input_tokens (a breakdown, not additive to totals).
    cache_creation_1h_input_tokens: int = 0

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens

    @property
    def total(self) -> int:
        return self.total_input + self.output_tokens

    def is_empty(self) -> bool:
        return self.total == 0

    def __iadd__(self, other: Usage) -> Usage:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_creation_1h_input_tokens += other.cache_creation_1h_input_tokens
        return self


def _first_int(d: dict, keys: tuple[str, ...]) -> int | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
    return None


def _merge_usage_dict(u: Usage, usage: dict, *, update_inputs: bool, update_output: bool) -> None:
    if update_inputs:
        iv = _first_int(usage, _INPUT_KEYS)
        if iv is not None:
            u.input_tokens = iv
        cr = _first_int(usage, _CACHE_READ_KEYS)
        if cr is not None:
            u.cache_read_input_tokens = cr
        cw = _first_int(usage, _CACHE_WRITE_KEYS)
        if cw is not None:
            u.cache_creation_input_tokens = cw
        cc = usage.get("cache_creation")
        if isinstance(cc, dict):
            oh = _first_int(cc, ("ephemeral_1h_input_tokens",))
            if oh is not None:
                u.cache_creation_1h_input_tokens = oh
    if update_output:
        ov = _first_int(usage, _OUTPUT_KEYS)
        if ov is not None:
            u.output_tokens = ov


def _apply_event(u: Usage, event: dict) -> None:
    """Fold one streaming event's usage into ``u``.

    ``message_start`` carries the prompt-side breakdown (and an initial output
    count); ``message_delta`` carries the running/final output count; Bedrock
    Converse emits a ``metadata`` event with the full breakdown.
    """
    etype = event.get("type")
    if etype == "message_start":
        msg = event.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
            _merge_usage_dict(u, msg["usage"], update_inputs=True, update_output=True)
    elif etype == "message_delta":
        if isinstance(event.get("usage"), dict):
            _merge_usage_dict(u, event["usage"], update_inputs=False, update_output=True)
    elif isinstance(event.get("metadata"), dict):
        meta = event["metadata"]
        if isinstance(meta.get("usage"), dict):
            _merge_usage_dict(u, meta["usage"], update_inputs=True, update_output=True)


def _iter_eventstream_payloads(body: bytes) -> Iterator[bytes]:
    """Yield the payload bytes of each AWS event-stream frame.

    Frame layout: ``[total_len u32][headers_len u32][prelude_crc u32]
    [headers][payload][message_crc u32]`` (all big-endian). CRCs are skipped;
    the length prefixes are sufficient to walk well-formed captures.
    """
    off, n = 0, len(body)
    while off + 12 <= n:
        total_len = int.from_bytes(body[off : off + 4], "big")
        headers_len = int.from_bytes(body[off + 4 : off + 8], "big")
        if total_len < 16 or off + total_len > n:
            break
        headers_end = off + 12 + headers_len
        payload_end = off + total_len - 4
        if headers_end > payload_end:
            break
        yield body[headers_end:payload_end]
        off += total_len


def _event_from_payload(payload: bytes) -> dict | None:
    try:
        obj = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    # Bedrock wraps each model event as base64 in a "bytes" field.
    raw = obj.get("bytes")
    if isinstance(raw, str):
        try:
            decoded = json.loads(base64.b64decode(raw))
        except (ValueError, UnicodeDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None
    return obj


def _parse_eventstream(body: bytes) -> Usage:
    u = Usage()
    for payload in _iter_eventstream_payloads(body):
        event = _event_from_payload(payload)
        if event is not None:
            _apply_event(u, event)
    return u


def _parse_sse(body: bytes) -> Usage:
    u = Usage()
    for raw in body.split(b"\n"):
        line = raw.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[len(b"data:") :].strip()
        if not data or data == b"[DONE]":
            continue
        try:
            event = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(event, dict):
            _apply_event(u, event)
    return u


def _parse_json(body: bytes) -> Usage:
    u = Usage()
    try:
        obj = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return u
    if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
        _merge_usage_dict(u, obj["usage"], update_inputs=True, update_output=True)
    return u


def extract_usage(content_type: str, body: bytes) -> Usage:
    """Extract token usage from one response body given its Content-Type.

    Returns a zeroed :class:`Usage` when nothing parseable is found; callers
    use :meth:`Usage.is_empty` to skip non-model responses.
    """
    ct = (content_type or "").lower()
    if "vnd.amazon.eventstream" in ct:
        return _parse_eventstream(body)
    if "text/event-stream" in ct:
        return _parse_sse(body)
    if "application/json" in ct:
        return _parse_json(body)
    # Unknown/missing content-type: best-effort sniff.
    head = body[:64].lstrip()
    if head.startswith(b"{"):
        return _parse_json(body)
    if b"data:" in body[:256]:
        return _parse_sse(body)
    return Usage()
