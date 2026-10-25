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

import io
import json
import os
import re
from collections.abc import Callable, Mapping
from contextlib import redirect_stderr, redirect_stdout
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


def _with_provider_transport(request_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Rewrite ``zen/`` and ``custom/`` requests into litellm's OpenAI-compatible form.

    Both rewrites are no-ops for any other model, so chaining them is safe on
    every call. ``custom/`` runs last because a Zen id can never be a custom one.
    """
    from lemoncrow.core.capabilities.providers.zen import apply_zen_transport
    from lemoncrow.pro.capabilities.model_setup.transport import apply_custom_transport

    return apply_custom_transport(apply_zen_transport(request_kwargs))


# Retained so any caller of the pre-custom-endpoint name keeps working.
_with_zen_transport = _with_provider_transport


def _content(response: Any) -> str:
    try:
        return str(response.choices[0].message.content or "")
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise LiteLLMUnavailable(f"Unexpected LiteLLM response shape: {exc}") from exc


#: What a retry says when the first reply to a JSON request held no JSON.
_JSON_ONLY = (
    "Your previous reply could not be read as JSON. Reply again with only the JSON object that answers the request: "
    "no prose before or after it, no Markdown, no code fences."
)
_FENCE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n?(.*?)```", re.DOTALL)
#: Most places in one reply a JSON value is looked for, so a reply full of braces cannot make parsing slow.
_MAX_JSON_STARTS = 200


def _parse_json(content: str) -> tuple[Any, str]:
    """``(value, "")`` for the JSON in a model reply, or ``(None, why)`` when it holds none.

    Models wrap JSON they were asked for in a Markdown fence or a sentence of prose even in JSON mode, and a
    provider that rejected ``response_format`` has no such mode at all. So after the strict parse the reply is
    searched, fenced blocks first: the first JSON object that decodes wins, and a list is taken only when there
    is no object (a ``[1]`` footnote in front of the answer must not be mistaken for it).
    """

    text = content.strip()
    try:
        return json.loads(text), ""
    except json.JSONDecodeError as exc:
        why = str(exc)
    decoder = json.JSONDecoder()
    candidates = [match.group(1) for match in _FENCE.finditer(text)] + [text]
    attempts = 0
    for opener in ("{", "["):
        for candidate in candidates:
            start = candidate.find(opener)
            while start != -1 and attempts < _MAX_JSON_STARTS:
                attempts += 1
                try:
                    value, _end = decoder.raw_decode(candidate, start)
                except json.JSONDecodeError:
                    start = candidate.find(opener, start + 1)
                    continue
                return value, ""
    return None, why


_ACCOUNT_ERROR = re.compile(
    r"AuthenticationError|PermissionDeniedError|RateLimitError|authentication_error|permission_error|rate_limit_error"
    r"|invalid[ _-]?x?-?api[ _-]?key|credit balance|insufficient[_ ](?:quota|credit|funds)|exceeded your current quota",
    re.IGNORECASE,
)


def is_account_error(exc: BaseException) -> bool:
    """Whether *exc* says the key, the account or the rate limit is the problem rather than the request's shape.

    Retrying such a request without ``response_format`` cannot help and is billed or counted against the limit again.
    """

    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in {401, 402, 403, 429}:
        return True
    return bool(_ACCOUNT_ERROR.search(f"{type(exc).__name__} {exc}"))


#: The most output one review call may ask for. A review answer is a small JSON object (a few hundred tokens); without a
#: limit LiteLLM sends the model's own maximum (128,000 tokens for current Claude models), which bounds nothing.
REVIEW_MAX_OUTPUT_TOKENS = 8192
REVIEW_MAX_OUTPUT_ENV = "LEMONCROW_REVIEW_MAX_OUTPUT_TOKENS"


def bounded_max_tokens(model: str | None, env: Mapping[str, str] | None = None) -> int:
    """The output-token limit for one review call on *model*.

    ``REVIEW_MAX_OUTPUT_TOKENS`` unless ``LEMONCROW_REVIEW_MAX_OUTPUT_TOKENS`` names another positive number, and never
    more than the model can produce when LiteLLM knows that limit.
    """

    raw = str((os.environ if env is None else env).get(REVIEW_MAX_OUTPUT_ENV) or "").strip()
    wanted = int(raw) if raw.isdigit() and int(raw) > 0 else REVIEW_MAX_OUTPUT_TOKENS
    limit = _model_output_limit(_resolve_model(model))
    return min(wanted, limit) if limit else wanted


def _model_output_limit(model: str) -> int:
    """The most output tokens LiteLLM's price table says *model* can produce; ``0`` when it does not know the model.

    Read from the table directly (``get_model_info`` prints a banner for an unknown id, and silencing a print means
    swapping ``sys.stdout``, which parallel review calls must not do).
    """

    try:
        table = _litellm_module().model_cost
    except Exception:
        return 0
    for key in (model, model.split("/", 1)[-1]):
        entry = table.get(key) if isinstance(table, dict) else None
        if isinstance(entry, dict):
            for field in ("max_output_tokens", "max_tokens"):
                limit = entry.get(field)
                if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
                    return limit
    return 0


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
            **_with_provider_transport(
                {
                    "model": chosen_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                }
            )
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
    request_kwargs = _with_provider_transport(request_kwargs)
    json_mode = json_schema is not None

    def complete(kwargs: dict[str, Any]) -> Any:
        nonlocal json_mode
        try:
            if json_mode:
                try:
                    return litellm.completion(**kwargs, response_format={"type": "json_object"})
                except Exception as exc:
                    if is_account_error(exc):
                        raise  # a rejected key or an empty balance fails the same way again: do not pay a second request
                    json_mode = False  # this provider rejects it; the retry below does not ask again
            return litellm.completion(**kwargs)
        except Exception as exc:
            if isinstance(exc, LiteLLMUnavailable):
                raise
            raise LiteLLMUnavailable(f"LiteLLM completion unavailable: {exc}") from exc

    response = complete(request_kwargs)
    responses = [response]
    content = _content(response)
    parsed_json: dict[str, Any] | None = None
    if json_schema is not None:
        parsed, why = _parse_json(content)
        if why:
            # One more try, saying so plainly: a reply of prose, or a refusal, often answers a direct instruction.
            retry = {
                **request_kwargs,
                "messages": [*request_kwargs["messages"], {"role": "user", "content": _JSON_ONLY}],
            }
            response = complete(retry)
            responses.append(response)
            content = _content(response)
            parsed, why = _parse_json(content)
            if why:
                raise LiteLLMUnavailable(f"LiteLLM returned invalid JSON, also after one retry: {why}")
        parsed_json = parsed if isinstance(parsed, dict) else {"value": parsed}
    costs = [_reported_cost(item) for item in responses]
    provider_cost = None if any(cost is None for cost in costs) else sum(cost for cost in costs if cost is not None)
    usages = [_field(item, "usage") for item in responses]
    return InternalLLMChatResult(
        content=content,
        parsed_json=parsed_json,
        model=chosen_model,
        reported_model=reported if isinstance(reported := _field(response, "model"), str) else "",
        request_id=str(_field(response, "id") or ""),
        # Every call that was made is paid for, so a retry adds to the tokens and the cost of the answer it produced.
        input_tokens=sum(_token_value(usage, "prompt_tokens") for usage in usages),
        output_tokens=sum(_token_value(usage, "completion_tokens") for usage in usages),
        cache_read_input_tokens=sum(_usage_detail(usage, "prompt_tokens_details", "cached_tokens") for usage in usages),
        cache_write_input_tokens=sum(
            _token_value(usage, "cache_creation_input_tokens")
            or _usage_detail(usage, "prompt_tokens_details", "cache_creation_tokens")
            for usage in usages
        ),
        cache_capability=_cache_capability(chosen_model=chosen_model, cache_metadata=cache_metadata),
        request_metadata=dict(cache_metadata or {}),
        provider_cost=provider_cost,
        currency="USD",
    )


def _reported_cost(response: Any) -> float | None:
    reported = _field(_field(response, "_hidden_params"), "response_cost")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool) and reported >= 0:
        return float(reported)
    return None


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


def chat_stream(
    messages: list[dict[str, Any]],
    *,
    on_delta: Callable[[str], None],
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> str | dict[str, Any]:
    """Stream LiteLLM completion chunks and preserve chat()'s parsed result contract."""
    litellm = _litellm_module()
    chosen_model = _resolve_model(model)
    request_kwargs: dict[str, Any] = {
        "model": chosen_model,
        "messages": messages,
        "stream": True,
    }
    if json_schema is not None:
        request_kwargs["response_format"] = {"type": "json_object"}
    request_kwargs = _with_provider_transport(request_kwargs)
    try:
        response = litellm.completion(**request_kwargs)
        chunks: list[str] = []
        for chunk in response:
            choices = _field(chunk, "choices")
            choice = choices[0] if isinstance(choices, list) and choices else None
            delta = _field(choice, "delta") if choice is not None else None
            content = _field(delta, "content")
            text = content if isinstance(content, str) else ""
            if not text:
                continue
            chunks.append(text)
            on_delta(text)
    except Exception as exc:
        if isinstance(exc, LiteLLMUnavailable):
            raise
        raise LiteLLMUnavailable(f"LiteLLM completion unavailable: {exc}") from exc
    content = "".join(chunks)
    if json_schema is None:
        return content
    parsed, why = _parse_json(content)
    if why:
        # No retry here: the caller has already been shown the deltas of this reply.
        raise LiteLLMUnavailable(f"LiteLLM returned invalid JSON: {why}")
    return parsed if isinstance(parsed, dict) else {"value": parsed}


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
        **_with_provider_transport(
            {
                "model": _resolve_model(model),
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
    )


__all__ = ["LiteLLMUnavailable", "chat", "chat_stream", "chat_with_result", "summarize", "tool_completion"]


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


def supports_vision(model: str) -> bool:
    """Best-effort vision-capability lookup for *model*, or False if unknowable.

    Lives here rather than at the call site so the litellm import stays behind
    the infra boundary (see tests/infra/test_no_external_llm_clients.py).
    LiteLLM prints a provider-help banner for unknown or custom model ids, and
    capability discovery is metadata: it must never reach a managed frontend's
    stdout/stderr, so both streams are swallowed.
    """
    try:
        litellm = _litellm_module()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return bool(litellm.supports_vision(model=model))
    except Exception:
        return False
