"""Thin OpenAI-compatible wrapper for Atelier's internal background processing.

Supports any server that implements the OpenAI Chat Completions API:
- OpenAI directly (default)
- OpenRouter (https://openrouter.ai/api/v1) — free model tier available
- opencode or any local OpenAI-compatible server (e.g., vllm, llama.cpp)

Environment variables
---------------------
ATELIER_LLM_BACKEND=openai     activate this client (or "openai_compatible")
ATELIER_OPENAI_BASE_URL        custom base URL; defaults to OpenAI's endpoint
ATELIER_OPENAI_API_KEY         API key; falls back to OPENAI_API_KEY
ATELIER_OPENAI_MODEL           model name; defaults to "gpt-4o-mini"

OpenRouter example
------------------
  ATELIER_LLM_BACKEND=openai
  ATELIER_OPENAI_BASE_URL=https://openrouter.ai/api/v1
  ATELIER_OPENAI_API_KEY=<your-openrouter-key>
  ATELIER_OPENAI_MODEL=meta-llama/llama-3-8b-instruct:free
"""

from __future__ import annotations

import json
import os
from typing import Any


class OpenAIClientUnavailable(RuntimeError):
    """Raised when the openai package or API endpoint is unavailable."""


def _openai_module() -> Any:
    try:
        import openai
    except ImportError as exc:
        raise OpenAIClientUnavailable("openai package is not installed; install atelier[cloud]") from exc
    return openai


def _resolve_client() -> Any:
    openai = _openai_module()
    base_url = os.environ.get("ATELIER_OPENAI_BASE_URL") or None
    api_key = (
        os.environ.get("ATELIER_OPENAI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or "no-key"  # local servers (llama.cpp, opencode) don't require a real key
    )
    return openai.OpenAI(api_key=api_key, base_url=base_url)


def _resolve_model() -> str:
    return os.environ.get("ATELIER_OPENAI_MODEL") or "gpt-4o-mini"


def summarize(text: str, *, model: str | None = None, max_tokens: int = 4096) -> str:
    """Summarize *text* using an OpenAI-compatible chat endpoint."""
    client = _resolve_client()
    chosen_model = model or _resolve_model()
    prompt = (
        "Summarize the following material for later engineering recall. Keep concrete file, "
        "command, error, and verification details.\n\n"
        f"Maximum output tokens: {max_tokens}\n\n{text}"
    )
    try:
        response = client.chat.completions.create(
            model=chosen_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
    except Exception as exc:
        raise OpenAIClientUnavailable(f"OpenAI-compatible API unavailable: {exc}") from exc
    return str(response.choices[0].message.content or "")


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> str | dict[str, Any]:
    """Call an OpenAI-compatible chat endpoint and optionally parse a JSON response."""
    client = _resolve_client()
    chosen_model = model or _resolve_model()
    try:
        kwargs: dict[str, Any] = {"model": chosen_model, "messages": messages}
        if json_schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        response = client.chat.completions.create(**kwargs)
    except Exception as exc:
        raise OpenAIClientUnavailable(f"OpenAI-compatible API unavailable: {exc}") from exc
    content = str(response.choices[0].message.content or "")
    if json_schema is None:
        return content
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise OpenAIClientUnavailable(f"API returned invalid JSON: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {"value": parsed}


__all__ = ["OpenAIClientUnavailable", "chat", "summarize"]
