"""OpenAI Responses wire adapter used by current Codex releases."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .adapter import _permission_note, _prepare_runtime_events, _usage_from_event
from .schemas import ChatMessage, ResponsesRequest


def _is_codex_host_context(text: str) -> bool:
    """Recognize the repo/environment envelope injected by the Codex host."""
    stripped = text.lstrip()
    agents_envelope = stripped.startswith("# AGENTS.md instructions for ")
    environment_envelope = stripped.startswith("<environment_context>")
    return (agents_envelope and "<environment_context>" in stripped) or environment_envelope


def _content_parts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return parts


def responses_input_to_messages(value: str | list[Any]) -> list[ChatMessage]:
    """Keep conversational text while removing the outer coding host's agent."""
    if isinstance(value, str):
        return [ChatMessage(role="user", content=value)] if value else []

    messages: list[ChatMessage] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("type", "message") != "message":
            continue
        role = item.get("role")
        if role not in {"user", "assistant"}:
            continue
        safe_parts = [text for text in _content_parts(item.get("content")) if not _is_codex_host_context(text)]
        text = "\n".join(part for part in safe_parts if part)
        if text:
            messages.append(ChatMessage(role=role, content=text))
    return messages


def _response_usage(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    cached = int(usage.get("prompt_tokens_details", {}).get("cached_tokens", 0) or 0)
    return {
        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


def _output_message(message_id: str, text: str, *, status: str) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "annotations": [],
                "logprobs": [],
                "text": text,
            }
        ],
        "phase": "final_answer",
    }


def _response_object(
    *,
    response_id: str,
    created_at: int,
    model: str,
    status: str,
    output: list[dict[str, Any]],
    usage: dict[str, Any] | None,
    max_output_tokens: int | None,
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "background": False,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": max_output_tokens,
        "model": model,
        "output": output,
        "parallel_tool_calls": False,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": usage,
        "user": None,
        "metadata": {},
    }


async def _responses_sse(
    events: AsyncIterator[Any],
    *,
    response_id: str,
    message_id: str,
    created_at: int,
    model: str,
    max_output_tokens: int | None,
) -> AsyncIterator[str]:
    sequence_number = 0

    def encode(event_type: str, **fields: Any) -> str:
        nonlocal sequence_number
        payload = {"type": event_type, "sequence_number": sequence_number, **fields}
        sequence_number += 1
        return f"event: {event_type}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"

    initial_response = _response_object(
        response_id=response_id,
        created_at=created_at,
        model=model,
        status="in_progress",
        output=[],
        usage=None,
        max_output_tokens=max_output_tokens,
    )
    yield encode("response.created", response=initial_response)
    yield encode("response.in_progress", response=initial_response)
    yield encode(
        "response.output_item.added",
        output_index=0,
        item={
            "id": message_id,
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
            "phase": "final_answer",
        },
    )
    empty_part = {"type": "output_text", "annotations": [], "logprobs": [], "text": ""}
    yield encode(
        "response.content_part.added",
        item_id=message_id,
        output_index=0,
        content_index=0,
        part=empty_part,
    )

    content_parts: list[str] = []
    usage: dict[str, Any] | None = None
    async for event in events:
        event_type = getattr(event, "type", "")
        if event_type == "assistant.delta":
            delta = getattr(event, "text", "")
        elif event_type == "permission.requested":
            delta = _permission_note(event)
        elif event_type == "error":
            yield encode(
                "error",
                code="lemoncrow_error",
                message=getattr(event, "message", "unknown error"),
                param=None,
            )
            return
        else:
            delta = ""

        if delta:
            content_parts.append(delta)
            yield encode(
                "response.output_text.delta",
                item_id=message_id,
                output_index=0,
                content_index=0,
                delta=delta,
                logprobs=[],
            )
        event_usage = _usage_from_event(event)
        if event_usage is not None:
            usage = event_usage

    text = "".join(content_parts)
    completed_part = {"type": "output_text", "annotations": [], "logprobs": [], "text": text}
    completed_item = _output_message(message_id, text, status="completed")
    yield encode(
        "response.output_text.done",
        item_id=message_id,
        output_index=0,
        content_index=0,
        text=text,
        logprobs=[],
    )
    yield encode(
        "response.content_part.done",
        item_id=message_id,
        output_index=0,
        content_index=0,
        part=completed_part,
    )
    yield encode("response.output_item.done", output_index=0, item=completed_item)
    completed_response = _response_object(
        response_id=response_id,
        created_at=created_at,
        model=model,
        status="completed",
        output=[completed_item],
        usage=_response_usage(usage),
        max_output_tokens=max_output_tokens,
    )
    yield encode("response.completed", response=completed_response)


async def run_response(runtime: Any, req: ResponsesRequest) -> Any:
    messages = responses_input_to_messages(req.input)
    if not messages:
        raise HTTPException(status_code=422, detail="input must contain a user message")

    session_id, events = await _prepare_runtime_events(
        runtime,
        messages,
        requested_model=req.model,
        max_output_tokens=req.max_output_tokens,
    )
    response_id = f"resp_{uuid.uuid4().hex}"
    message_id = f"msg_{uuid.uuid4().hex}"
    created_at = int(time.time())

    if req.stream:
        sse = _responses_sse(
            events,
            response_id=response_id,
            message_id=message_id,
            created_at=created_at,
            model=req.model,
            max_output_tokens=req.max_output_tokens,
        )

        async def stream() -> AsyncIterator[str]:
            try:
                async for line in sse:
                    yield line
            finally:
                runtime.drop_session(session_id)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    content_parts: list[str] = []
    usage: dict[str, Any] | None = None
    try:
        async for event in events:
            event_type = getattr(event, "type", "")
            if event_type == "assistant.delta":
                content_parts.append(getattr(event, "text", ""))
            elif event_type == "permission.requested":
                content_parts.append(_permission_note(event))
            elif event_type == "error":
                raise HTTPException(status_code=500, detail=getattr(event, "message", "unknown error"))
            event_usage = _usage_from_event(event)
            if event_usage is not None:
                usage = event_usage
    finally:
        runtime.drop_session(session_id)

    item = _output_message(message_id, "".join(content_parts), status="completed")
    return JSONResponse(
        _response_object(
            response_id=response_id,
            created_at=created_at,
            model=req.model,
            status="completed",
            output=[item],
            usage=_response_usage(usage),
            max_output_tokens=req.max_output_tokens,
        )
    )


__all__ = ["responses_input_to_messages", "run_response"]
