"""Thin LiteLLM wrapper for LemonCrow's internal background processing.

LiteLLM gives native access to providers that do NOT speak the OpenAI protocol
— Bedrock, Vertex AI / Gemini, Azure OpenAI, Anthropic, Cohere — through one
unified ``completion`` call, using each provider's native cloud auth. Use this
backend when running inside AWS / Azure / GCP and you want managed models with
cloud-native credentials, without standing up a proxy sidecar.

Environment variables
---------------------
LEMONCROW_LLM_BACKEND=litellm     activate this client
LEMONCROW_LITELLM_MODEL           provider-prefixed model string, e.g.
                                "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
                                "vertex_ai/gemini-1.5-pro",
                                "azure/<deployment-name>",
                                "gpt-4o-mini" (default)

Provider credentials are read by LiteLLM from the standard cloud env vars:
  Bedrock  — AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION_NAME (or IAM role)
  Vertex   — GOOGLE_APPLICATION_CREDENTIALS / VERTEXAI_PROJECT / VERTEXAI_LOCATION
  Azure    — AZURE_API_KEY / AZURE_API_BASE / AZURE_API_VERSION
"""

from __future__ import annotations

import json
import os
from typing import Any

from lemoncrow.infra.internal_llm.exceptions import LiteLLMUnavailable
from lemoncrow.infra.internal_llm.result import InternalLLMChatResult


def _litellm_module() -> Any:
    try:
        import litellm
    except ImportError as exc:
        raise LiteLLMUnavailable("litellm package is not installed; install lemoncrow[litellm]") from exc
    return litellm


def _resolve_model(model: str | None) -> str:
    return model or os.environ.get("LEMONCROW_LITELLM_MODEL") or "gpt-4o-mini"


def _content(response: Any) -> str:
    try:
        return str(response.choices[0].message.content or "")
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise LiteLLMUnavailable(f"Unexpected LiteLLM response shape: {exc}") from exc


def _field(source: Any, name: str) -> Any:
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _token_value(source: Any, name: str) -> int:
    value = _field(source, name)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return int(max(0.0, value))
    return 0


def _usage_detail(usage: Any, detail_name: str, token_name: str) -> int:
    detail = _field(usage, detail_name)
    if detail is None:
        return 0
    return _token_value(detail, token_name)


def summarize(text: str, *, model: str | None = None, max_tokens: int = 4096) -> str:
    """Summarize *text* via LiteLLM."""
    litellm = _litellm_module()
    chosen_model = _resolve_model(model)
    prompt = (
        "Summarize the following material for later engineering recall, in telegraphic "
        "style: drop articles, copulas, and filler; keep concrete file, command, error, "
        "and verification details.\n\n"
        f"Maximum output tokens: {max_tokens}\n\n{text}"
    )
    try:
        response = litellm.completion(
            model=chosen_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
    except Exception as exc:
        if isinstance(exc, LiteLLMUnavailable):
            raise
        raise LiteLLMUnavailable(f"LiteLLM completion unavailable: {exc}") from exc
    return _content(response)


def chat_with_result(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
    cache_metadata: dict[str, Any] | None = None,
    api_key: str | None = None,
    extra_kwargs: dict[str, Any] | None = None,
) -> InternalLLMChatResult:
    """Call LiteLLM and optionally parse a JSON response.

    JSON mode is attempted via ``response_format``; providers that reject it
    (some Bedrock / Vertex models) transparently fall back to a plain call and
    the content is JSON-parsed afterward.

    ``extra_kwargs`` are forwarded verbatim to ``litellm.completion`` for
    provider-specific parameters (e.g. ``seed`` for OpenAI prefix caching,
    ``extra_body={"cachedContent": ...}`` for Gemini context caching,
    ``max_tokens`` for a connectivity probe). This keeps every litellm call on
    the user's path routed through this single infra wrapper.
    """
    litellm = _litellm_module()
    chosen_model = _resolve_model(model)
    request_messages = _apply_cache_control(messages, chosen_model=chosen_model, cache_metadata=cache_metadata)
    request_kwargs: dict[str, Any] = {"model": chosen_model, "messages": request_messages}
    if api_key:
        request_kwargs["api_key"] = api_key
    if extra_kwargs:
        request_kwargs.update(extra_kwargs)
    try:
        if json_schema is None:
            response = litellm.completion(**request_kwargs)
        else:
            try:
                response = litellm.completion(
                    **request_kwargs,
                    response_format={"type": "json_object"},
                )
            except Exception:  # noqa: BLE001 - provider may reject response_format; retry plain
                response = litellm.completion(**request_kwargs)
    except Exception as exc:
        if isinstance(exc, LiteLLMUnavailable):
            raise
        raise LiteLLMUnavailable(f"LiteLLM completion unavailable: {exc}") from exc
    content = _content(response)
    parsed_json: dict[str, Any] | None = None
    if json_schema is not None:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LiteLLMUnavailable(f"LiteLLM returned invalid JSON: {exc}") from exc
        parsed_json = parsed if isinstance(parsed, dict) else {"value": parsed}
    usage = _field(response, "usage")
    return InternalLLMChatResult(
        content=content,
        parsed_json=parsed_json,
        model=chosen_model,
        request_id=str(_field(response, "id") or ""),
        input_tokens=_token_value(usage, "prompt_tokens"),
        output_tokens=_token_value(usage, "completion_tokens"),
        cache_read_input_tokens=_usage_detail(usage, "prompt_tokens_details", "cached_tokens"),
        cache_write_input_tokens=_token_value(usage, "cache_creation_input_tokens")
        or _usage_detail(usage, "prompt_tokens_details", "cache_creation_tokens"),
        cache_capability=_cache_capability(chosen_model=chosen_model, cache_metadata=cache_metadata),
        request_metadata=dict(cache_metadata or {}),
    )


def chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> str | dict[str, Any]:
    result = chat_with_result(messages, model=model, json_schema=json_schema)
    if json_schema is None:
        return result.content
    return dict(result.parsed_json or {})


def tool_completion(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str | None = None,
    tool_choice: str = "auto",
) -> Any:
    """Run a tool-calling completion and return the raw LiteLLM response.

    Kept in this module so the ``litellm`` import stays within the infra
    boundary; callers (e.g. the agentic reviewer) inspect
    ``response.choices[0].message.tool_calls`` on the returned object.
    """
    litellm = _litellm_module()
    return litellm.completion(
        model=_resolve_model(model),
        messages=messages,
        tools=tools,
        tool_choice=tool_choice,
    )


__all__ = ["LiteLLMUnavailable", "chat", "chat_with_result", "summarize", "tool_completion"]


def _apply_cache_control(
    messages: list[dict[str, Any]],
    *,
    chosen_model: str,
    cache_metadata: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not cache_metadata or not _supports_anthropic_cache_control(chosen_model):
        return messages
    patched = [dict(message) for message in messages]
    if not patched:
        return patched
    first = dict(patched[0])
    content = first.get("content")
    if first.get("role") != "system" or not isinstance(content, str) or not content.strip():
        return patched
    first["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
    patched[0] = first
    return patched


def _cache_capability(*, chosen_model: str, cache_metadata: dict[str, Any] | None) -> str:
    if not cache_metadata:
        return "none"
    return "explicit" if _supports_anthropic_cache_control(chosen_model) else "hint_only"


def _supports_anthropic_cache_control(chosen_model: str) -> bool:
    normalized = chosen_model.strip().lower()
    return "anthropic" in normalized or "claude" in normalized
