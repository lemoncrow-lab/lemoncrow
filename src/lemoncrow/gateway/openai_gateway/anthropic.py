"""Anthropic Messages compatibility for Claude Code as a thin frontend."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .adapter import (
    _permission_note,
    _prepare_runtime_events,
    _usage_from_event,
)
from .schemas import ChatMessage


class AnthropicMessageRequest(BaseModel):
    model: str = "lemoncrow"
    messages: list[dict[str, Any]]
    max_tokens: int = Field(default=4096, ge=1)
    stream: bool = False
    system: str | list[dict[str, Any]] | None = None

    model_config = {"extra": "ignore"}


class AnthropicCountRequest(BaseModel):
    model: str = "lemoncrow"
    messages: list[dict[str, Any]]
    system: str | list[dict[str, Any]] | None = None

    model_config = {"extra": "ignore"}


def _strip_claude_host_reminders(text: str) -> str:
    """Remove Claude Code's leading host-only <system-reminder> envelopes."""
    cleaned = text.lstrip()
    closing = "</system-reminder>"
    while cleaned.startswith("<system-reminder>"):
        end = cleaned.find(closing)
        if end < 0:
            break
        cleaned = cleaned[end + len(closing) :].lstrip()
    return cleaned


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return _strip_claude_host_reminders(content)
    if isinstance(content, list):
        text = " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
        return _strip_claude_host_reminders(text)
    return ""


def _chat_messages(messages: list[dict[str, Any]]) -> list[ChatMessage]:
    converted: list[ChatMessage] = []
    for raw in messages:
        role = str(raw.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        text = _content_text(raw.get("content"))
        if text:
            converted.append(ChatMessage(role=role, content=text))
    return converted


def _estimated_input_tokens(messages: list[dict[str, Any]], system: Any = None) -> int:
    rendered = json.dumps(
        {"system": system, "messages": messages},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return max(1, len(rendered) // 4)


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def _anthropic_sse(
    runtime: Any,
    req: AnthropicMessageRequest,
    message_id: str,
) -> AsyncIterator[str]:
    session_id, events = await _prepare_runtime_events(
        runtime,
        _chat_messages(req.messages),
        requested_model=None,
        max_output_tokens=req.max_tokens,
    )
    input_tokens = _estimated_input_tokens(req.messages, req.system)
    output_tokens = 0
    had_error = False
    try:
        yield _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": req.model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0},
                },
            },
        )
        yield _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        async for event in events:
            event_type = getattr(event, "type", "")
            if event_type == "assistant.delta":
                text = str(getattr(event, "text", ""))
                output_tokens += max(0, len(text) // 4)
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            elif event_type == "permission.requested":
                text = _permission_note(event)
                output_tokens += max(0, len(text) // 4)
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            elif event_type == "error":
                had_error = True
                yield _sse(
                    "error",
                    {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": getattr(event, "message", "unknown error"),
                        },
                    },
                )
            usage = _usage_from_event(event)
            if usage is not None:
                input_tokens = usage["prompt_tokens"]
                output_tokens = usage["completion_tokens"]

        if not had_error:
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
            yield _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": output_tokens},
                },
            )
            yield _sse("message_stop", {"type": "message_stop"})
    finally:
        runtime.drop_session(session_id)


async def run_anthropic_message(runtime: Any, req: AnthropicMessageRequest) -> Any:
    if not req.messages:
        raise HTTPException(status_code=422, detail="messages must not be empty")
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    if req.stream:
        return StreamingResponse(
            _anthropic_sse(runtime, req, message_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    session_id, events = await _prepare_runtime_events(
        runtime,
        _chat_messages(req.messages),
        requested_model=None,
        max_output_tokens=req.max_tokens,
    )
    content_parts: list[str] = []
    input_tokens = _estimated_input_tokens(req.messages, req.system)
    output_tokens = 0
    try:
        async for event in events:
            event_type = getattr(event, "type", "")
            if event_type == "assistant.delta":
                content_parts.append(str(getattr(event, "text", "")))
            elif event_type == "permission.requested":
                content_parts.append(_permission_note(event))
            elif event_type == "error":
                raise HTTPException(
                    status_code=500,
                    detail=getattr(event, "message", "unknown error"),
                )
            usage = _usage_from_event(event)
            if usage is not None:
                input_tokens = usage["prompt_tokens"]
                output_tokens = usage["completion_tokens"]
    finally:
        runtime.drop_session(session_id)

    text = "".join(content_parts)
    if not output_tokens:
        output_tokens = max(0, len(text) // 4)
    return JSONResponse(
        {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": req.model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        }
    )


def count_anthropic_tokens(req: AnthropicCountRequest) -> dict[str, int]:
    return {"input_tokens": _estimated_input_tokens(req.messages, req.system)}


__all__ = [
    "AnthropicCountRequest",
    "AnthropicMessageRequest",
    "count_anthropic_tokens",
    "run_anthropic_message",
]
