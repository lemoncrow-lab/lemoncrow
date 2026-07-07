"""Pydantic schemas mirroring the OpenAI chat completions wire format.

We define these locally so the gateway has no hard dep on the ``openai`` package.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant" | "system" | "tool"
    content: str | list[Any] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None

    model_config = {"extra": "ignore"}


class DeltaContent(BaseModel):
    role: str | None = None
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    refusal: str | None = None


class DeltaChoice(BaseModel):
    index: int = 0
    delta: DeltaContent
    finish_reason: str | None = None
    logprobs: None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[DeltaChoice]
    usage: dict[str, Any] | None = None


class ModelObject(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "atelier"


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelObject]
