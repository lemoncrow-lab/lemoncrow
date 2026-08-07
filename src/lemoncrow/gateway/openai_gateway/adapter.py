"""Translate OpenAI-compatible requests into LemonCrow-owned runtime turns."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatMessage,
    DeltaChoice,
    DeltaContent,
)

if TYPE_CHECKING:
    from lemoncrow.gateway.cli.events import LemonCrowEvent

_VIRTUAL_MODELS = {
    "lemoncrow",
    "lemoncrow-default",
    "lemoncrow-default/latest",
    "lc/lemoncrow",
    "lc/lemoncrow-default",
}


def is_virtual_model(model: str | None) -> bool:
    normalized = (model or "").strip().lower()
    return normalized in _VIRTUAL_MODELS or normalized.startswith("lc/lemoncrow")


def _decode_host_string(text: str) -> str:
    """Undo a whole-string JSON envelope emitted by some coding frontends."""
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return text
        if isinstance(decoded, str):
            return decoded
    return text


def _message_text(msg: ChatMessage) -> str:
    if isinstance(msg.content, str):
        return _decode_host_string(msg.content)
    if isinstance(msg.content, list):
        text = " ".join(
            str(part.get("text", "")) for part in msg.content if isinstance(part, dict) and part.get("text")
        )
        return _decode_host_string(text)
    return ""


def openai_messages_to_lemoncrow(
    messages: list[ChatMessage],
) -> tuple[str, list[dict[str, Any]]]:
    """Return the newest user turn and safe prior user/assistant history.

    Host system prompts and host tool transcripts are deliberately dropped.
    Mature coding CLIs routinely send tens of kilobytes of their own system and
    tool schema text; LemonCrow owns those surfaces and must not pay for them a
    second time at the upstream provider.
    """
    last_user: ChatMessage | None = None
    for message in messages:
        if message.role == "user":
            last_user = message
    if last_user is None:
        raise ValueError("No user message found in the request")

    prior: list[dict[str, Any]] = []
    for message in messages:
        if message is last_user:
            continue
        if message.role not in {"user", "assistant"}:
            continue
        if message.role == "assistant" and message.tool_calls:
            continue
        text = _message_text(message)
        if text:
            prior.append({"role": message.role, "content": text})
    return _message_text(last_user), prior


def _permission_note(event: Any) -> str:
    action: str = getattr(event, "action", "tool call")
    risk: str = getattr(event, "risk", "medium") or "medium"
    return f"\n\n[LemonCrow: executing {action} ({risk} risk) autonomously]\n\n"


def _usage_from_event(event: Any) -> dict[str, Any] | None:
    if getattr(event, "type", "") != "context.usage.updated":
        return None
    fresh = int(getattr(event, "input_tokens", 0) or 0)
    cache_read = int(getattr(event, "cache_read_tokens", 0) or 0)
    cache_write = int(getattr(event, "cache_write_tokens", 0) or 0)
    output = int(getattr(event, "output_tokens", 0) or 0)
    return {
        "prompt_tokens": fresh + cache_read + cache_write,
        "completion_tokens": output,
        "total_tokens": fresh + cache_read + cache_write + output,
        "prompt_tokens_details": {
            "cached_tokens": cache_read,
            "cache_write_tokens": cache_write,
        },
    }


async def _prepare_runtime_events(
    runtime: Any,
    messages: list[ChatMessage],
    *,
    requested_model: str | None,
    max_output_tokens: int | None,
) -> tuple[str, AsyncIterator[Any]]:
    try:
        last_user_text, prior_history = openai_messages_to_lemoncrow(messages)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    session_id = str(uuid.uuid4())
    runtime.restore_session(session_id, prior_history)

    context = ""
    primer_metadata: dict[str, Any] = {}
    if not any(message.get("role") == "user" for message in prior_history):
        root = getattr(runtime, "root", None)
        project_root = getattr(runtime, "project_root", None)
        if isinstance(root, Path) and isinstance(project_root, Path):
            from lemoncrow.pro.capabilities.owned_agent_session.primer_cache import cached_task_primer

            primer = await asyncio.to_thread(
                cached_task_primer,
                last_user_text,
                project_root,
                root,
                retrieval_mode=os.environ.get("LEMONCROW_LOCAL_RETRIEVAL", "auto"),
                local_retrieval_model=os.environ.get("LEMONCROW_LOCAL_RETRIEVAL_MODEL", ""),
                optimization_mode=os.environ.get("LEMONCROW_OPTIMIZATION_MODE", "shadow"),
            )
            context = primer.text
            primer_metadata = primer.optimization_metadata()

    events = runtime.handle_user_message(
        session_id,
        last_user_text,
        model_override=None if is_virtual_model(requested_model) else requested_model,
        context=context,
        max_output_tokens=max_output_tokens,
        primer_metadata=primer_metadata,
    )
    return session_id, events


def _route_label(event: Any) -> str | None:
    """Human-visible ``provider/model`` (or bare model) from a route.selected event.

    This is the one place that turns the runtime's internal routing decision
    (which vendor -- local, Zen, or a keyed provider -- actually served the
    turn) into the string a client displays, so "which backend just ran" is
    never a guess.
    """
    model_id = getattr(event, "model", None)
    if not model_id:
        return None
    provider = getattr(event, "provider", None)
    return f"{provider}/{model_id}" if provider else str(model_id)


async def lemoncrow_events_to_sse(
    events: AsyncIterator[LemonCrowEvent],
    model: str,
    chunk_id: str | None = None,
) -> AsyncIterator[str]:
    """Consume the full runtime stream and emit OpenAI SSE.

    The runtime's final usage/cache events arrive after the assistant message;
    consuming through exhaustion is required both for correct accounting and
    for session compaction/finalization.

    ``model`` is the client-requested id (almost always the virtual
    "lemoncrow" model). It is only a placeholder: the runtime always emits a
    ``route.selected`` event before any assistant text, naming the real
    backend (local/Zen/keyed vendor) chosen for this turn, and every chunk
    from that point on reports that real model instead.
    """
    chunk_id = chunk_id or f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    usage: dict[str, Any] | None = None
    had_error = False
    resolved_model = model
    role_sent = False

    def _chunk(
        delta: DeltaContent,
        finish_reason: str | None = None,
        chunk_usage: dict[str, Any] | None = None,
    ) -> str:
        chunk = ChatCompletionChunk(
            id=chunk_id,
            created=created,
            model=resolved_model,
            choices=[DeltaChoice(index=0, delta=delta, finish_reason=finish_reason)],
            usage=chunk_usage,
        )
        return f"data: {chunk.model_dump_json()}\n\n"

    async for event in events:
        event_type = getattr(event, "type", "")
        if event_type == "route.selected":
            resolved_model = _route_label(event) or resolved_model
            continue
        if not role_sent:
            yield _chunk(DeltaContent(role="assistant"))
            role_sent = True
        if event_type == "assistant.delta":
            yield _chunk(DeltaContent(content=getattr(event, "text", "")))
        elif event_type == "permission.requested":
            yield _chunk(DeltaContent(content=_permission_note(event)))
        elif event_type == "error":
            message = getattr(event, "message", "unknown error")
            payload = json.dumps({"error": {"message": message, "type": "lemoncrow_error"}})
            yield f"data: {payload}\n\n"
            had_error = True
        else:
            event_usage = _usage_from_event(event)
            if event_usage is not None:
                usage = event_usage

    if not role_sent:
        yield _chunk(DeltaContent(role="assistant"))
    if not had_error:
        yield _chunk(DeltaContent(content=""), finish_reason="stop", chunk_usage=usage)
    yield "data: [DONE]\n\n"


async def run_chat_completion(runtime: Any, req: ChatCompletionRequest) -> Any:
    if not req.messages:
        raise HTTPException(status_code=422, detail="messages must not be empty")

    max_output_tokens = req.max_tokens or req.max_completion_tokens
    session_id, events = await _prepare_runtime_events(
        runtime,
        req.messages,
        requested_model=req.model,
        max_output_tokens=max_output_tokens,
    )
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    response_model = req.model or "lemoncrow"

    if req.stream:
        sse = lemoncrow_events_to_sse(events, model=response_model, chunk_id=chunk_id)

        async def _stream() -> AsyncIterator[str]:
            try:
                async for line in sse:
                    yield line
            finally:
                runtime.drop_session(session_id)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    content_parts: list[str] = []
    usage: dict[str, Any] | None = None
    resolved_model = response_model
    try:
        async for event in events:
            event_type = getattr(event, "type", "")
            if event_type == "route.selected":
                resolved_model = _route_label(event) or resolved_model
            elif event_type == "assistant.delta":
                content_parts.append(getattr(event, "text", ""))
            elif event_type == "permission.requested":
                content_parts.append(_permission_note(event))
            elif event_type == "error":
                raise HTTPException(
                    status_code=500,
                    detail=getattr(event, "message", "unknown error"),
                )
            event_usage = _usage_from_event(event)
            if event_usage is not None:
                usage = event_usage
    finally:
        runtime.drop_session(session_id)

    return JSONResponse(
        {
            "id": chunk_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": resolved_model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "".join(content_parts)},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }
    )


__all__ = [
    "_permission_note",
    "_prepare_runtime_events",
    "_usage_from_event",
    "is_virtual_model",
    "lemoncrow_events_to_sse",
    "openai_messages_to_lemoncrow",
    "run_chat_completion",
]
