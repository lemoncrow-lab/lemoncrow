"""Adapter: convert between OpenAI chat messages and Atelier NDJSON events.

Three pieces:
1. openai_messages_to_atelier(): extract the last user turn + prior history
2. atelier_events_to_sse(): stream AtelierEvents as OpenAI SSE delta chunks
3. run_chat_completion(): shared /v1/chat/completions handler used by both
   the standalone gateway app and the integrated Atelier service
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
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
    from atelier.gateway.cli.events import AtelierEvent


def openai_messages_to_atelier(
    messages: list[ChatMessage],
) -> tuple[str, list[dict[str, Any]]]:
    """Extract the last user message and return the prior conversation history.

    Returns:
        (last_user_text, prior_history)  — prior_history is a list of
        ``{"role": ..., "content": ...}`` dicts that can be injected into
        the runtime session so context is preserved across requests.

    Raises:
        ValueError: when the message list contains no user messages.
    """
    user_messages = [m for m in messages if m.role == "user"]
    if not user_messages:
        raise ValueError("No user message found in the request")

    last_user = user_messages[-1]

    # Normalise content: lists (multi-modal) → concatenate text parts
    def _text(msg: ChatMessage) -> str:
        if isinstance(msg.content, str):
            return msg.content
        if isinstance(msg.content, list):
            return " ".join(part.get("text", "") for part in msg.content if isinstance(part, dict))
        return ""

    last_user_text = _text(last_user)

    # Prior history excludes the last user message (identity, not position, so a
    # request that ends with a non-user turn doesn't duplicate it). The runtime
    # owns its own system prompt, so client ``system``/``tool`` roles must not be
    # forwarded as authoritative turns: system text is folded into a user turn,
    # tool results are dropped. Assistant turns that carry ``tool_calls`` can't
    # be flattened to a string without losing structure, so they're dropped too.
    prior: list[dict[str, Any]] = []
    for msg in messages:
        if msg is last_user:
            continue
        if msg.role == "tool":
            continue
        if msg.role == "assistant" and msg.tool_calls:
            continue
        if msg.role == "system":
            text = _text(msg)
            if text:
                prior.append({"role": "user", "content": text})
            continue
        prior.append({"role": msg.role, "content": _text(msg)})

    return last_user_text, prior


def _permission_note(event: Any) -> str:
    """Render a permission.requested event as an inline assistant note."""
    action: str = getattr(event, "action", "tool call")
    risk: str = getattr(event, "risk", "medium") or "medium"
    return f"\n\n[Atelier: executing {action} ({risk} risk) autonomously]\n\n"


async def atelier_events_to_sse(
    events: AsyncIterator[AtelierEvent],
    model: str,
    chunk_id: str | None = None,
) -> AsyncIterator[str]:
    """Convert a stream of AtelierEvents to OpenAI SSE chunks.

    Yields ``data: <json>\\n\\n`` lines followed by ``data: [DONE]\\n\\n``.
    Skips session-internal events (route selection, cache stats, etc.) that
    callers don't need.
    """
    if chunk_id is None:
        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    created = int(time.time())

    def _chunk(delta: DeltaContent, finish_reason: str | None = None) -> str:
        chunk = ChatCompletionChunk(
            id=chunk_id,
            created=created,
            model=model,
            choices=[DeltaChoice(index=0, delta=delta, finish_reason=finish_reason)],
        )
        return f"data: {chunk.model_dump_json()}\n\n"

    async for event in events:
        ev_type: str = getattr(event, "type", "")

        # ── streaming text token ─────────────────────────────────────────────
        if ev_type == "assistant.delta":
            yield _chunk(DeltaContent(content=getattr(event, "text", "")))

        # ── final message → close stream ─────────────────────────────────────
        elif ev_type == "assistant.message":
            yield _chunk(DeltaContent(content=""), finish_reason="stop")
            yield "data: [DONE]\n\n"
            return

        # ── tool call requested → Atelier runs it server-side; emit nothing ──
        # The resulting text arrives as later assistant.delta events. We do not
        # forward OpenAI tool_calls deltas: the client cannot execute them and
        # the stream finishes with finish_reason='stop', not 'tool_calls'.

        # ── permission / approval prompt → inject a system note as text ──────
        elif ev_type == "permission.requested":
            yield _chunk(DeltaContent(content=_permission_note(event)))

        # ── error → surface to caller then stop ──────────────────────────────
        elif ev_type == "error":
            message: str = getattr(event, "message", "unknown error")
            error_payload = json.dumps({"error": {"message": message, "type": "atelier_error"}})
            yield f"data: {error_payload}\n\n"
            yield "data: [DONE]\n\n"
            return

        # ── everything else is internal (routing, cache stats, etc.) → skip ──

    # Stream ended without an explicit AssistantMessage (e.g. interrupted)
    yield _chunk(DeltaContent(content=""), finish_reason="stop")
    yield "data: [DONE]\n\n"


async def run_chat_completion(runtime: Any, req: ChatCompletionRequest) -> Any:
    """Handle one /v1/chat/completions request against an InteractiveRuntime.

    Shared by the standalone gateway app and the integrated Atelier service so
    the wire protocol has exactly one implementation. The per-request session
    is dropped once the response finishes — the OpenAI protocol is stateless.
    """
    if not req.messages:
        raise HTTPException(status_code=422, detail="messages must not be empty")

    try:
        last_user_text, prior_history = openai_messages_to_atelier(req.messages)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    session_id = str(uuid.uuid4())
    runtime._sessions[session_id] = prior_history

    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    model = req.model or ""

    events_gen = runtime.handle_user_message(
        session_id,
        last_user_text,
        model_override=model or None,
    )

    if req.stream:
        sse_gen = atelier_events_to_sse(events_gen, model=model or "atelier", chunk_id=chunk_id)

        async def _stream() -> AsyncIterator[str]:
            try:
                async for line in sse_gen:
                    yield line
            finally:
                runtime._sessions.pop(session_id, None)

        return StreamingResponse(_stream(), media_type="text/event-stream")

    # Buffered (non-streaming) — consume the runtime events directly
    content_parts: list[str] = []
    try:
        async for event in events_gen:
            ev_type = getattr(event, "type", "")
            if ev_type == "assistant.delta":
                content_parts.append(getattr(event, "text", ""))
            elif ev_type == "permission.requested":
                content_parts.append(_permission_note(event))
            elif ev_type == "error":
                raise HTTPException(status_code=500, detail=getattr(event, "message", "unknown error"))
    finally:
        runtime._sessions.pop(session_id, None)

    return JSONResponse(
        {
            "id": chunk_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "".join(content_parts),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": None,
        }
    )
