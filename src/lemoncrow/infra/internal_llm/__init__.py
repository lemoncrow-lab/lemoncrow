"""Internal local-model helpers for background-only processing.

Backend selection via ``LEMONCROW_LLM_BACKEND`` environment variable:

- ``none``              — disabled; all calls raise InternalLLMError (default)
- ``ollama``            — local Ollama server
- ``openai``            — OpenAI API or any OpenAI-compatible endpoint
- ``openai_compatible`` — alias for ``openai``
- ``litellm``           — native multi-provider (Bedrock, Vertex/Gemini, Azure)

See ``openai_client.py`` for OpenRouter / opencode / local vllm configuration,
and ``litellm_client.py`` for AWS / Azure / GCP cloud-native model access.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from lemoncrow.infra.internal_llm.exceptions import (
    InternalLLMError,
    LiteLLMUnavailable,
    OllamaUnavailable,
)
from lemoncrow.infra.internal_llm.logprobs import (
    chunk_entropy,
    logprobs,
    token_surprisals,
)

__all__ = [
    "InternalLLMError",
    "LiteLLMUnavailable",
    "OllamaUnavailable",
    "chat",
    "chat_stream",
    "chunk_entropy",
    "configured_model",
    "logprobs",
    "summarize",
    "token_surprisals",
]


def _backend() -> str:
    return os.environ.get("LEMONCROW_LLM_BACKEND", "none").lower().strip()


def _require_intelligence_enabled() -> None:
    """The policy half of the gate: a disabled or invalid policy refuses. It never reads the AI budget.

    Used where no call may be made (a cache hit sends nothing). Every call that sends something to a model enters
    ``ai_budget.model_call`` instead, which adds the personal AI budget (P8-4).
    """

    from lemoncrow.pro.capabilities.review.intelligence_runtime import require_intelligence_enabled

    require_intelligence_enabled()


def _model_call() -> Any:
    """The gate and meter of one call that sends something to a model: refuses before anything is sent."""

    from lemoncrow.pro.capabilities.review.ai_budget import model_call

    return model_call()


def configured_model() -> tuple[str, str] | None:
    """The ``(backend, model)`` a default call would use, or ``None`` when disabled.

    For labelling generated text with what produced it; it does not probe the
    endpoint.
    """
    backend = _backend()
    if backend == "openai_compatible":
        backend = "openai"
    if backend == "openai":
        from lemoncrow.infra.internal_llm.openai_client import _resolve_model as _openai_model

        return backend, _openai_model()
    if backend == "litellm":
        from lemoncrow.infra.internal_llm.litellm_client import _resolve_model as _litellm_model

        return backend, _litellm_model(None)
    if backend == "ollama":
        from lemoncrow.infra.internal_llm.ollama_client import _resolve_model as _ollama_model

        return backend, _ollama_model(None)
    return None


def chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
    backend: str | None = None,
) -> str | dict[str, Any]:
    """Call the configured or explicitly selected internal LLM chat endpoint."""
    with _model_call() as meter:
        backend = (backend or _backend()).lower().strip()
        if backend == "openai_compatible":
            backend = "openai"
        if backend not in {"none", "ollama", "openai", "litellm"}:
            raise InternalLLMError(f"Unsupported internal LLM backend: {backend}")
        if backend == "none":
            raise InternalLLMError(
                "Internal LLM disabled; set LEMONCROW_LLM_BACKEND=ollama or LEMONCROW_LLM_BACKEND=openai to enable"
            )
        meter.route(backend, model or "")

        try:
            if backend in ("openai", "openai_compatible"):
                from lemoncrow.infra.internal_llm.openai_client import chat as _chat

                result = _chat(messages, model=model, json_schema=json_schema)
            elif backend == "litellm":
                from lemoncrow.infra.internal_llm.litellm_client import chat as _chat

                result = _chat(messages, model=model, json_schema=json_schema)
            else:
                from lemoncrow.infra.internal_llm.ollama_client import chat as _chat

                result = _chat(messages, model=model, json_schema=json_schema)
        except Exception as exc:
            if isinstance(exc, InternalLLMError):
                raise
            raise InternalLLMError(f"Internal LLM ({backend}) failed: {exc}") from exc
        return result


def chat_stream(
    messages: list[dict[str, Any]],
    *,
    on_delta: Callable[[str], None],
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
    backend: str | None = None,
) -> str | dict[str, Any]:
    """Stream raw assistant text while preserving the normal chat return contract."""
    with _model_call() as meter:
        backend = (backend or _backend()).lower().strip()
        if backend == "openai_compatible":
            backend = "openai"
        if backend not in {"none", "ollama", "openai", "litellm"}:
            raise InternalLLMError(f"Unsupported internal LLM backend: {backend}")
        if backend == "none":
            raise InternalLLMError(
                "Internal LLM disabled; set LEMONCROW_LLM_BACKEND=ollama or LEMONCROW_LLM_BACKEND=openai to enable"
            )
        meter.route(backend, model or "")
        try:
            if backend == "openai":
                from lemoncrow.infra.internal_llm.openai_client import chat_stream as _chat_stream
            elif backend == "litellm":
                from lemoncrow.infra.internal_llm.litellm_client import chat_stream as _chat_stream
            else:
                from lemoncrow.infra.internal_llm.ollama_client import chat_stream as _chat_stream
            result = _chat_stream(messages, on_delta=on_delta, model=model, json_schema=json_schema)
        except Exception as exc:
            if isinstance(exc, InternalLLMError):
                raise
            raise InternalLLMError(f"Internal LLM ({backend}) failed: {exc}") from exc
        return result


def summarize(text: str, *, model: str | None = None, max_tokens: int = 4096) -> str:
    """Summarize text using the configured internal LLM.

    Identical ``(text, model, max_tokens, backend)`` inputs are memoized in a
    persistent on-disk cache (shared across processes and sessions) so repeated
    background summaries don't re-pay provider tokens; disable with
    ``LEMONCROW_INTERNAL_LLM_CACHE=0``. A cache hit sends nothing, so only a
    computed summary is gated by the AI budget and metered.
    """
    _require_intelligence_enabled()
    backend = _backend()
    if backend == "none":
        raise InternalLLMError(
            "Internal LLM disabled; set LEMONCROW_LLM_BACKEND=ollama or LEMONCROW_LLM_BACKEND=openai to enable"
        )

    def _compute() -> str:
        with _model_call() as meter:
            meter.route(backend, model or "")
            try:
                if backend in ("openai", "openai_compatible"):
                    from lemoncrow.infra.internal_llm.openai_client import summarize as _summarize
                elif backend == "litellm":
                    from lemoncrow.infra.internal_llm.litellm_client import summarize as _summarize
                else:
                    from lemoncrow.infra.internal_llm.ollama_client import summarize as _summarize
                summary = _summarize(text, model=model, max_tokens=max_tokens)
            except Exception as exc:
                if isinstance(exc, InternalLLMError):
                    raise
                raise InternalLLMError(f"Internal LLM ({backend}) failed: {exc}") from exc
            return summary

    from lemoncrow.infra.internal_llm.cache import cached_summarize

    return cached_summarize(
        text,
        model=model,
        max_tokens=max_tokens,
        backend=backend,
        compute=_compute,
    )
