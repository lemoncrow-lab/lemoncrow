"""In-process rate limiter for Atelier provider/model requests.

Configured via ``~/.atelier/providers.json`` under each provider's
``rate_limit`` key::

    {
      "anthropic": {
        "api_key": "...",
        "rate_limit": {
          "requests_per_minute": 60,
          "tokens_per_minute": 100000
        }
      }
    }

For per-model overrides, use the model id as the key::

    {
      "openai/gpt-4o": {
        "rate_limit": {"requests_per_minute": 30}
      }
    }

Limits are applied per-process (in-memory). Requests over the limit are
held (not dropped) until the window replenishes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RateLimit:
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None


@dataclass
class _Window:
    limit: RateLimit
    _request_times: list[float] = field(default_factory=list, repr=False)
    _token_counts: list[tuple[float, int]] = field(default_factory=list, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def _prune(self, now: float, window: float = 60.0) -> None:
        cutoff = now - window
        self._request_times = [t for t in self._request_times if t > cutoff]
        self._token_counts = [(t, n) for t, n in self._token_counts if t > cutoff]

    def _requests_in_window(self) -> int:
        return len(self._request_times)

    def _tokens_in_window(self) -> int:
        return sum(n for _, n in self._token_counts)

    async def acquire(self, estimated_tokens: int = 0) -> None:
        """Block until a slot is available, then reserve it."""
        while True:
            # Hold the lock only for the check-and-reserve, never across the
            # sleep, so waiters don't serialize behind each other.
            async with self._lock:
                now = time.monotonic()
                self._prune(now)
                rpm = self.limit.requests_per_minute
                tpm = self.limit.tokens_per_minute
                rpm_ok = rpm is None or self._requests_in_window() < rpm
                tpm_ok = tpm is None or self._tokens_in_window() + estimated_tokens <= tpm
                if rpm_ok and tpm_ok:
                    self._request_times.append(now)
                    if estimated_tokens and tpm is not None:
                        self._token_counts.append((now, estimated_tokens))
                    return
            # Sleep a short time then retry
            await asyncio.sleep(0.2)

    def record_tokens(self, tokens: int) -> None:
        """Record actual token usage after a response completes."""
        if not self.limit.tokens_per_minute:
            return
        now = time.monotonic()
        self._token_counts.append((now, tokens))


# Global registry: key = "provider" or "provider/model_id"
_limiters: dict[str, _Window] = {}
_init_lock = asyncio.Lock()

# Bare model-name prefixes → provider, for models given without a litellm prefix.
_BARE_MODEL_PROVIDERS: dict[str, str] = {
    "claude": "anthropic",
    "gpt": "openai",
    "o1": "openai",
    "o3": "openai",
    "o4": "openai",
    "gemini": "google",
}


def _limiter_key(model: str) -> str:
    """Return the best matching limiter key for a model ID."""
    from .config import LITELLM_PREFIX

    # Exact model match first, then provider prefix
    if model in _limiters:
        return model
    # e.g. "anthropic/claude-haiku-4-5" → check "anthropic"
    if "/" in model:
        provider = model.split("/")[0]
        if provider in _limiters:
            return provider
    # litellm prefixes that differ from the provider name, e.g. "together_ai/x" → "together"
    for provider, prefix in LITELLM_PREFIX.items():
        if model.startswith(prefix) and provider in _limiters:
            return provider
    # e.g. "claude-haiku-4-5" → "anthropic"
    for prefix, provider in _BARE_MODEL_PROVIDERS.items():
        if model.startswith(prefix) and provider in _limiters:
            return provider
    return ""


async def init_from_config(raw: dict[str, Any]) -> None:
    """Build the limiter registry from providers.json content."""
    global _limiters
    async with _init_lock:
        _limiters = {}
        for key, value in raw.items():
            if key.startswith("_") or not isinstance(value, dict):
                continue
            rl_dict = value.get("rate_limit")
            if not rl_dict or not isinstance(rl_dict, dict):
                continue
            limit = RateLimit(
                requests_per_minute=rl_dict.get("requests_per_minute"),
                tokens_per_minute=rl_dict.get("tokens_per_minute"),
            )
            if limit.requests_per_minute or limit.tokens_per_minute:
                _limiters[key] = _Window(limit=limit)
                logger.debug("rate limiter registered: %s → %s", key, limit)


async def acquire(model: str, estimated_tokens: int = 0) -> None:
    """Acquire a rate-limit slot for the given model. No-op if not configured."""
    key = _limiter_key(model)
    if key:
        await _limiters[key].acquire(estimated_tokens)


def record_tokens(model: str, tokens: int) -> None:
    """Record actual token usage after a request completes."""
    key = _limiter_key(model)
    if key:
        _limiters[key].record_tokens(tokens)


def get_status() -> dict[str, Any]:
    """Return current rate-limiter state for all registered keys."""
    now = time.monotonic()
    result = {}
    for key, window in _limiters.items():
        window._prune(now)
        result[key] = {
            "requests_per_minute": window.limit.requests_per_minute,
            "tokens_per_minute": window.limit.tokens_per_minute,
            "requests_in_window": window._requests_in_window(),
            "tokens_in_window": window._tokens_in_window(),
        }
    return result
