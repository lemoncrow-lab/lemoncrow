"""Internal local-model helpers for background-only processing.

Backend selection via ``ATELIER_LLM_BACKEND`` environment variable:

- ``ollama``            — local Ollama server (default)
- ``openai``            — OpenAI API or any OpenAI-compatible endpoint
- ``openai_compatible`` — alias for ``openai``

See ``openai_client.py`` for OpenRouter / opencode / local vllm configuration.
"""

from __future__ import annotations

import os
from typing import Any

from atelier.infra.internal_llm.ollama_client import OllamaUnavailable


def _backend() -> str:
    return os.environ.get("ATELIER_LLM_BACKEND", "ollama").lower().strip()


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> str | dict[str, Any]:
    if _backend() in ("openai", "openai_compatible"):
        from atelier.infra.internal_llm.openai_client import chat as _chat

        return _chat(messages, model=model, json_schema=json_schema)
    from atelier.infra.internal_llm.ollama_client import chat as _chat

    return _chat(messages, model=model, json_schema=json_schema)


def summarize(text: str, *, model: str | None = None, max_tokens: int = 4096) -> str:
    if _backend() in ("openai", "openai_compatible"):
        from atelier.infra.internal_llm.openai_client import summarize as _summarize

        return _summarize(text, model=model, max_tokens=max_tokens)
    from atelier.infra.internal_llm.ollama_client import summarize as _summarize

    return _summarize(text, model=model, max_tokens=max_tokens)


__all__ = ["OllamaUnavailable", "chat", "summarize"]
