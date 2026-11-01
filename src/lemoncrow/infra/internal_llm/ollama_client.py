"""Thin Ollama wrapper for LemonCrow's internal background processing."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from lemoncrow.infra.internal_llm.exceptions import OllamaUnavailable


def _ollama_module() -> Any:
    try:
        import ollama
    except ImportError as exc:  # pragma: no cover - exercised by tests via monkeypatch
        raise OllamaUnavailable("ollama package is not installed; install lemoncrow[smart]") from exc
    return ollama


def _resolve_model(model: str | None) -> str:
    configured = os.environ.get("LEMONCROW_OLLAMA_MODEL", "").strip()
    if model:
        return model
    if configured:
        return configured
    return "qwen3.6:27b"


def _format(json_schema: dict[str, Any]) -> dict[str, Any] | str:
    """Ollama's ``format``: the schema itself, so decoding is held to its keys and types; plain JSON mode without one."""

    return json_schema or "json"


def _no_answer(done_reason: str) -> str:
    why = (
        ": it reached its context limit before answering (a thinking model can spend all of it reasoning)"
        if done_reason == "length"
        else ""
    )
    return f"Ollama returned no answer (done_reason={done_reason or 'unknown'}){why}"


def summarize(text: str, *, model: str | None = None, max_tokens: int = 4096) -> str:
    """Summarize text with a local Ollama model."""
    chosen_model = _resolve_model(model)
    prompt = (
        "Summarize the following material for later engineering recall, in telegraphic "
        "style: drop articles, copulas, and filler; keep concrete file, command, error, "
        "and verification details.\n\n"
        f"Maximum output tokens: {max_tokens}\n\n{text}"
    )
    try:
        response = _ollama_module().generate(
            model=chosen_model,
            prompt=prompt,
            options={"num_predict": max_tokens},
        )
    except Exception as exc:  # pragma: no cover - depends on local server
        if isinstance(exc, OllamaUnavailable):
            raise
        raise OllamaUnavailable(f"Ollama server unavailable: {exc}") from exc
    if isinstance(response, dict):
        return str(response.get("response", ""))
    return str(getattr(response, "response", ""))


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> str | dict[str, Any]:
    """Call Ollama chat and optionally parse a JSON response."""
    chosen_model = _resolve_model(model)
    options: dict[str, Any] = {}
    try:
        if json_schema is None:
            response = _ollama_module().chat(model=chosen_model, messages=messages, options=options)
        else:
            response = _ollama_module().chat(
                model=chosen_model,
                messages=messages,
                format=_format(json_schema),
                options=options,
            )
    except TypeError as exc:
        if json_schema is None:
            raise OllamaUnavailable(f"Ollama server unavailable: {exc}") from exc
        try:
            legacy_options = {**options, "format": "json"}
            response = _ollama_module().chat(model=chosen_model, messages=messages, options=legacy_options)
        except Exception as exc:  # pragma: no cover - depends on local server
            raise OllamaUnavailable(f"Ollama server unavailable: {exc}") from exc
    except Exception as exc:  # pragma: no cover - depends on local server
        if isinstance(exc, OllamaUnavailable):
            raise
        raise OllamaUnavailable(f"Ollama server unavailable: {exc}") from exc
    message = response.get("message", {}) if isinstance(response, dict) else getattr(response, "message", {})
    content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
    if json_schema is None:
        return str(content)
    if not str(content or "").strip():
        done = response.get("done_reason") if isinstance(response, dict) else getattr(response, "done_reason", "")
        raise OllamaUnavailable(_no_answer(str(done or "")))
    try:
        parsed = json.loads(str(content))
    except json.JSONDecodeError as exc:
        raise OllamaUnavailable(f"Ollama returned invalid JSON: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def chat_stream(
    messages: list[dict[str, str]],
    *,
    on_delta: Callable[[str], None],
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> str | dict[str, Any]:
    """Stream Ollama chat chunks and return the same parsed result as chat()."""
    chosen_model = _resolve_model(model)
    kwargs: dict[str, Any] = {
        "model": chosen_model,
        "messages": messages,
        "options": {},
        "stream": True,
    }
    if json_schema is not None:
        kwargs["format"] = _format(json_schema)
    try:
        response = _ollama_module().chat(**kwargs)
    except TypeError as exc:
        if json_schema is None:
            raise OllamaUnavailable(f"Ollama server unavailable: {exc}") from exc
        legacy = dict(kwargs)
        legacy.pop("format", None)
        legacy["options"] = {"format": "json"}
        try:
            response = _ollama_module().chat(**legacy)
        except Exception as inner:
            raise OllamaUnavailable(f"Ollama server unavailable: {inner}") from inner
    except Exception as exc:
        if isinstance(exc, OllamaUnavailable):
            raise
        raise OllamaUnavailable(f"Ollama server unavailable: {exc}") from exc

    chunks: list[str] = []
    try:
        for part in response:
            message = part.get("message", {}) if isinstance(part, dict) else getattr(part, "message", {})
            content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
            text = str(content or "")
            if not text:
                continue
            chunks.append(text)
            on_delta(text)
    except Exception as exc:
        if isinstance(exc, OllamaUnavailable):
            raise
        raise OllamaUnavailable(f"Ollama stream unavailable: {exc}") from exc
    content = "".join(chunks)
    if json_schema is None:
        return content
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise OllamaUnavailable(f"Ollama returned invalid JSON: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {"value": parsed}


__all__ = ["OllamaUnavailable", "chat", "chat_stream", "summarize"]
