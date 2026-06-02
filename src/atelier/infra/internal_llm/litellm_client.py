"""Thin LiteLLM wrapper for Atelier's internal background processing.

LiteLLM gives native access to providers that do NOT speak the OpenAI protocol
— Bedrock, Vertex AI / Gemini, Azure OpenAI, Anthropic, Cohere — through one
unified ``completion`` call, using each provider's native cloud auth. Use this
backend when running inside AWS / Azure / GCP and you want managed models with
cloud-native credentials, without standing up a proxy sidecar.

Environment variables
---------------------
ATELIER_LLM_BACKEND=litellm     activate this client
ATELIER_LITELLM_MODEL           provider-prefixed model string, e.g.
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

from atelier.infra.internal_llm.exceptions import LiteLLMUnavailable


def _litellm_module() -> Any:
    try:
        import litellm
    except ImportError as exc:
        raise LiteLLMUnavailable("litellm package is not installed; install atelier[litellm]") from exc
    return litellm


def _resolve_model(model: str | None) -> str:
    return model or os.environ.get("ATELIER_LITELLM_MODEL") or "gpt-4o-mini"


def _content(response: Any) -> str:
    try:
        return str(response.choices[0].message.content or "")
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise LiteLLMUnavailable(f"Unexpected LiteLLM response shape: {exc}") from exc


def summarize(text: str, *, model: str | None = None, max_tokens: int = 4096) -> str:
    """Summarize *text* via LiteLLM."""
    litellm = _litellm_module()
    chosen_model = _resolve_model(model)
    prompt = (
        "Summarize the following material for later engineering recall. Keep concrete file, "
        "command, error, and verification details.\n\n"
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


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> str | dict[str, Any]:
    """Call LiteLLM and optionally parse a JSON response.

    JSON mode is attempted via ``response_format``; providers that reject it
    (some Bedrock / Vertex models) transparently fall back to a plain call and
    the content is JSON-parsed afterward.
    """
    litellm = _litellm_module()
    chosen_model = _resolve_model(model)
    try:
        if json_schema is None:
            response = litellm.completion(model=chosen_model, messages=messages)
        else:
            try:
                response = litellm.completion(
                    model=chosen_model,
                    messages=messages,
                    response_format={"type": "json_object"},
                )
            except Exception:  # noqa: BLE001 - provider may reject response_format; retry plain
                response = litellm.completion(model=chosen_model, messages=messages)
    except Exception as exc:
        if isinstance(exc, LiteLLMUnavailable):
            raise
        raise LiteLLMUnavailable(f"LiteLLM completion unavailable: {exc}") from exc
    content = _content(response)
    if json_schema is None:
        return content
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LiteLLMUnavailable(f"LiteLLM returned invalid JSON: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {"value": parsed}


__all__ = ["LiteLLMUnavailable", "chat", "summarize"]
