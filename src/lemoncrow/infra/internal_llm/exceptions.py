"""Centralized exceptions for the internal LLM subsystem."""

from __future__ import annotations


class InternalLLMError(RuntimeError):
    """Base exception for all internal LLM failures (Ollama, OpenAI, etc.)."""


class OllamaUnavailable(InternalLLMError):
    """Raised when the optional Ollama dependency or server is unavailable."""


class OpenAIClientUnavailable(InternalLLMError):
    """Raised when the openai package or API endpoint is unavailable."""


class LiteLLMUnavailable(InternalLLMError):
    """Raised when the optional litellm dependency or a provider call fails."""


# --------------------------------------------------------------------------- #
# a provider that refuses: the fix is outside LemonCrow, so say what it is in one line
# --------------------------------------------------------------------------- #

#: Phrases :func:`describe_refusal` writes and :func:`refusal_kind` finds again in a stored error string.
KEY_REJECTED = "rejected the API key"
OUT_OF_CREDIT = "has no credit left"
RATE_LIMITED = "rate limit reached"

_KEY_VARIABLE = {"Anthropic": "ANTHROPIC_API_KEY", "OpenAI": "OPENAI_API_KEY"}
_BILLING_PAGE = {
    "Anthropic": "console.anthropic.com/settings/billing",
    "OpenAI": "platform.openai.com/settings/organization/billing",
}
_CREDIT_WORDS = ("credit balance", "insufficient_quota", "exceeded your current quota", "insufficient credit")
_KEY_WORDS = ("invalid x-api-key", "incorrect api key", "invalid api key", "authentication_error")
_QUOTED_LIMIT = 100


def describe_refusal(provider: str, status: int | None, message: str = "") -> str:
    """One plain sentence for a refusal the person fixes at the provider (a bad key, no credit, a rate limit), or ``""``.

    The same sentence whichever route made the call (the SDK-free HTTPS route, LiteLLM or the OpenAI SDK), so the review
    can show it alone and a script can find it with :func:`refusal_kind`. The provider's own words follow in quotes when
    they are short; anything longer is left out rather than cut mid-sentence.
    """

    lowered = message.lower()
    if status == 402 or any(word in lowered for word in _CREDIT_WORDS):
        what = f"{provider} says your account {OUT_OF_CREDIT}"
        fix = f"add credit at {_BILLING_PAGE.get(provider, 'your provider billing page')}"
    elif status in (401, 403) or any(word in lowered for word in _KEY_WORDS):
        variable = _KEY_VARIABLE.get(provider, "your API key")
        what = f"{provider} {KEY_REJECTED}"
        fix = f"check {variable}"
    elif status == 429:
        what = f"{provider} {RATE_LIMITED}"
        fix = "wait a minute and run it again"
    else:
        return ""
    code = f" (HTTP {status})" if status else ""
    reason = " ".join(message.split())
    quoted = f' ("{reason}")' if reason and len(reason) <= _QUOTED_LIMIT else ""
    return f"{what}{code}: {fix}.{quoted}"


def refusal_kind(text: str) -> str:
    """``key``, ``credit`` or ``limit`` when *text* holds a sentence :func:`describe_refusal` wrote, else ``""``."""

    if KEY_REJECTED in text:
        return "key"
    if OUT_OF_CREDIT in text:
        return "credit"
    if RATE_LIMITED in text:
        return "limit"
    return ""


def refusal_from_exception(provider: str, exc: BaseException) -> str:
    """:func:`describe_refusal` for an SDK exception (it carries ``status_code`` and ``message``), or ``""``."""

    status = getattr(exc, "status_code", None)
    named = str(getattr(exc, "llm_provider", "") or "").strip().lower() or provider.lower()
    shown = {"anthropic": "Anthropic", "openai": "OpenAI"}.get(named) or named.capitalize() or "The provider"
    return describe_refusal(shown, status if isinstance(status, int) else None, str(getattr(exc, "message", "") or exc))
