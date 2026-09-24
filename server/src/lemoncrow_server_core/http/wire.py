"""Wire encoding, bounded reads and non-accumulating streaming.

Three jobs:

**Bounded ingress.** :func:`read_capped_body` checks the declared
``Content-Length`` first (a cheap reject) and then caps the streamed read, so a
missing or lying header cannot smuggle an oversized payload past the limit.

**Strict parsing.** Every field accessor refuses rather than coerces. A tool
API that silently accepts ``"3"`` for an integer revision is a tool API that
eventually serves the wrong revision.

**Streaming that never accumulates.** :func:`iter_json_chunks` drives
``json.JSONEncoder.iterencode`` and flushes a bounded buffer, so the encoded
form of a large tool result never exists in memory in one piece. The frames are
NDJSON (or SSE): a ``begin`` frame carrying the metadata an agent needs before
the body, then ``result_chunk`` frames whose ``text`` values concatenate to the
JSON document, then a terminal ``end`` or ``error`` frame. A stream that dies
mid-body is therefore detectable by the absence of ``end`` rather than by
parsing a truncated object.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final

from aiohttp import web

from ..errors import AgentAction, ErrorCode, ServerError

__all__ = [
    "NDJSON_MEDIA_TYPE",
    "SSE_MEDIA_TYPE",
    "iter_json_chunks",
    "json_response",
    "ndjson_frame",
    "parse_json_object",
    "read_capped_body",
    "require_int",
    "require_object",
    "require_str",
    "sse_frame",
]

NDJSON_MEDIA_TYPE: Final[str] = "application/x-ndjson"
SSE_MEDIA_TYPE: Final[str] = "text/event-stream"

_ENCODER: Final[json.JSONEncoder] = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))


def json_response(payload: Mapping[str, Any], *, status: int = 200) -> web.Response:
    return web.json_response(
        dict(payload),
        status=status,
        dumps=lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")),
    )


async def read_capped_body(request: web.Request, limit: int) -> bytes:
    """Read the body, refusing anything over ``limit`` bytes."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise ServerError(
                    ErrorCode.REQUEST_TOO_LARGE,
                    "request body exceeds the server limit",
                    details={"limit": limit, "declared": int(declared)},
                    action=AgentAction.SPLIT_REQUEST,
                )
        except ValueError:
            pass
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.content.iter_chunked(64 * 1024):
        received += len(chunk)
        if received > limit:
            raise ServerError(
                ErrorCode.REQUEST_TOO_LARGE,
                "request body exceeds the server limit",
                details={"limit": limit},
                action=AgentAction.SPLIT_REQUEST,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def parse_json_object(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "request body is not valid JSON",
        ) from exc
    if not isinstance(parsed, dict):
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "request body must be a JSON object",
        )
    return parsed


def require_str(body: Mapping[str, Any], field: str, *, max_len: int = 4096) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"{field} must be a non-empty string",
            details={"field": field},
        )
    if len(value) > max_len:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"{field} exceeds {max_len} characters",
            details={"field": field, "limit": max_len},
        )
    return value


def require_int(body: Mapping[str, Any], field: str, *, minimum: int = 0) -> int:
    value = body.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"{field} must be an integer",
            details={"field": field},
        )
    if value < minimum:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"{field} must be >= {minimum}",
            details={"field": field, "minimum": minimum},
        )
    return value


def require_object(body: Mapping[str, Any], field: str) -> dict[str, Any]:
    value = body.get(field, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"{field} must be a JSON object",
            details={"field": field},
        )
    return value


def require_array(body: Mapping[str, Any], field: str, *, max_len: int) -> list[Any]:
    value = body.get(field, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"{field} must be an array",
            details={"field": field},
        )
    if len(value) > max_len:
        raise ServerError(
            ErrorCode.REQUEST_TOO_LARGE,
            f"{field} exceeds {max_len} entries",
            details={"field": field, "limit": max_len},
            action=AgentAction.SPLIT_REQUEST,
        )
    return value


def require_str_array(body: Mapping[str, Any], field: str, *, max_len: int) -> tuple[str, ...]:
    entries = require_array(body, field, max_len=max_len)
    out: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry:
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                f"{field} must contain non-empty strings",
                details={"field": field},
            )
        out.append(entry)
    return tuple(out)


def iter_json_chunks(payload: Mapping[str, Any] | Sequence[Any], chunk_bytes: int) -> Iterator[str]:
    """Yield the JSON encoding of ``payload`` in bounded pieces.

    Uses the incremental encoder, so the complete encoded document is never
    materialized. Pieces are split on encoder-fragment boundaries and are not
    individually valid JSON; concatenating every piece yields the document.
    """
    buffer: list[str] = []
    size = 0
    for fragment in _ENCODER.iterencode(payload):
        buffer.append(fragment)
        size += len(fragment)
        if size >= chunk_bytes:
            yield "".join(buffer)
            buffer.clear()
            size = 0
    if buffer:
        yield "".join(buffer)


def ndjson_frame(frame: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(frame), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def sse_frame(frame: Mapping[str, Any]) -> bytes:
    body = json.dumps(dict(frame), ensure_ascii=False, separators=(",", ":"))
    return f"data: {body}\n\n".encode()
