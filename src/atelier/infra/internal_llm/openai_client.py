"""Thin OpenAI-compatible wrapper for Atelier's internal background processing.

Supports any server that implements the OpenAI Chat Completions API:
- OpenAI directly (default)
- OpenRouter (https://openrouter.ai/api/v1) — free model tier available
- opencode or any local OpenAI-compatible server (e.g., vllm, some local LLMs)

Environment variables
---------------------
ATELIER_LLM_BACKEND=openai     activate this client (or "openai_compatible")
ATELIER_OPENAI_BASE_URL        custom base URL; defaults to OpenAI's endpoint
ATELIER_OPENAI_API_KEY         API key; falls back to OPENAI_API_KEY
ATELIER_OPENAI_MODEL           model name; defaults to "gpt-4o-mini"
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from atelier.infra.internal_llm.exceptions import OpenAIClientUnavailable
from atelier.infra.internal_llm.result import InternalLLMChatResult


def _openai_module() -> Any:
    try:
        import openai
    except ImportError as exc:
        raise OpenAIClientUnavailable("openai package is not installed; install atelier[cloud]") from exc
    return openai


# Reuse OpenAI clients (and their underlying httpx connection pools) across
# calls, keyed by (base_url, api_key). Rebuilding a client per call paid a fresh
# TCP + TLS handshake on every internal-LLM request (compaction, recall). The
# cache stays tiny in practice; clear it wholesale if a rotating key ever grows
# it past a small bound.
_CLIENT_CACHE: dict[tuple[Any, ...], Any] = {}
_CLIENT_LOCK = threading.Lock()


def _resolve_client() -> Any:
    openai = _openai_module()
    base_url = os.environ.get("ATELIER_OPENAI_BASE_URL") or None
    api_key = os.environ.get("ATELIER_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        if base_url is None:
            raise OpenAIClientUnavailable(
                "no OpenAI API key found; set ATELIER_OPENAI_API_KEY or OPENAI_API_KEY "
                "(or ATELIER_OPENAI_BASE_URL for a keyless local server)"
            )
        # Keyless local OpenAI-compatible servers accept any placeholder key.
        api_key = "no-key"
    cache_key = (base_url, api_key)
    with _CLIENT_LOCK:
        client = _CLIENT_CACHE.get(cache_key)
        if client is None:
            if len(_CLIENT_CACHE) >= 8:
                _CLIENT_CACHE.clear()
            client = openai.OpenAI(api_key=api_key, base_url=base_url)
            _CLIENT_CACHE[cache_key] = client
    return client


def _resolve_model() -> str:
    return os.environ.get("ATELIER_OPENAI_MODEL") or "gpt-4o-mini"


def _token_value(source: Any, name: str) -> int:
    value = source.get(name) if isinstance(source, dict) else getattr(source, name, 0)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return int(max(0.0, value))
    return 0


def _usage_detail(usage: Any, detail_name: str, token_name: str) -> int:
    detail = usage.get(detail_name) if isinstance(usage, dict) else getattr(usage, detail_name, None)
    if detail is None:
        return 0
    return _token_value(detail, token_name)


def summarize(text: str, *, model: str | None = None, max_tokens: int = 4096) -> str:
    """Summarize *text* using an OpenAI-compatible chat endpoint."""
    client = _resolve_client()
    chosen_model = model or _resolve_model()
    prompt = (
        "Summarize the following material for later engineering recall, in telegraphic "
        "style: drop articles, copulas, and filler; keep concrete file, command, error, "
        "and verification details.\n\n"
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


def chat_with_result(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
    cache_metadata: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> InternalLLMChatResult:
    """Call an OpenAI-compatible chat endpoint and return content plus usage metadata."""
    client = _resolve_client()
    chosen_model = model or _resolve_model()
    try:
        kwargs: dict[str, Any] = {"model": chosen_model, "messages": messages}
        if json_schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout
        prompt_cache_key = str((cache_metadata or {}).get("prompt_cache_key") or "").strip()
        if prompt_cache_key:
            kwargs["extra_body"] = {"prompt_cache_key": prompt_cache_key}
        response = client.chat.completions.create(**kwargs)
    except Exception as exc:
        raise OpenAIClientUnavailable(f"OpenAI-compatible API unavailable: {exc}") from exc
    content = str(response.choices[0].message.content or "")
    parsed_json: dict[str, Any] | None = None
    if json_schema is not None:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OpenAIClientUnavailable(f"API returned invalid JSON: {exc}") from exc
        parsed_json = parsed if isinstance(parsed, dict) else {"value": parsed}
    usage = getattr(response, "usage", None)
    return InternalLLMChatResult(
        content=content,
        parsed_json=parsed_json,
        model=chosen_model,
        request_id=str(getattr(response, "id", "") or ""),
        input_tokens=_token_value(usage, "prompt_tokens"),
        output_tokens=_token_value(usage, "completion_tokens"),
        cache_read_input_tokens=_usage_detail(usage, "prompt_tokens_details", "cached_tokens"),
        cache_write_input_tokens=_token_value(usage, "cache_creation_input_tokens")
        or _usage_detail(usage, "prompt_tokens_details", "cache_creation_tokens"),
        cache_capability="explicit" if prompt_cache_key else ("hint_only" if cache_metadata else "none"),
        request_metadata={"prompt_cache_key": prompt_cache_key} if prompt_cache_key else dict(cache_metadata or {}),
    )


def chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> str | dict[str, Any]:
    """Call an OpenAI-compatible chat endpoint and optionally parse a JSON response."""
    result = chat_with_result(messages, model=model, json_schema=json_schema)
    if json_schema is None:
        return result.content
    return dict(result.parsed_json or {})


__all__ = ["OpenAIClientUnavailable", "chat", "chat_with_result", "summarize"]
