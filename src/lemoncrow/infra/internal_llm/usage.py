"""What a model answer reported about its own use, handed to whoever is metering the call (P8-4).

The clients return text to the facade, so their token counts and cost would be lost on the way. Each client calls
:func:`note_response` the moment a provider's answer is in hand (before it parses the answer, so a billed answer that
fails parsing is still counted), and :func:`collect_usage` lets the one caller that meters the call read them. With no
collector active nothing is kept.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

__all__ = ["UsageCollector", "UsageNote", "collect_usage", "note_response"]


@dataclass(frozen=True, slots=True)
class UsageNote:
    """One provider answer: its model, tokens and the cost the provider reported (``None``: none reported)."""

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    provider_cost: float | None = None

    @property
    def reported(self) -> bool:
        """Whether the answer told us anything about its use."""

        return self.provider_cost is not None or self.input_tokens > 0 or self.output_tokens > 0


@dataclass(slots=True)
class UsageCollector:
    notes: list[UsageNote] = field(default_factory=list)


_collector: contextvars.ContextVar[UsageCollector | None] = contextvars.ContextVar(
    "lemoncrow_usage_collector", default=None
)


@contextmanager
def collect_usage() -> Iterator[UsageCollector]:
    collector = UsageCollector()
    token = _collector.set(collector)
    try:
        yield collector
    finally:
        _collector.reset(token)


def _get(source: Any, name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _count(source: Any, *names: str) -> int:
    for name in names:
        value = _get(source, name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return 0


def note_response(model: str, usage: Any, *, provider_cost: float | None = None) -> None:
    """Record one provider answer. *usage* is the SDK's or the JSON reply's usage object (OpenAI, Anthropic or LiteLLM
    names), or ``None`` when the answer carried none: it is then a call with nothing reported."""

    collector = _collector.get()
    if collector is None:
        return
    details = _get(usage, "prompt_tokens_details")
    cost = provider_cost if isinstance(provider_cost, (int, float)) and not isinstance(provider_cost, bool) else None
    collector.notes.append(
        UsageNote(
            model=model,
            input_tokens=_count(usage, "prompt_tokens", "input_tokens"),
            output_tokens=_count(usage, "completion_tokens", "output_tokens"),
            cache_read_tokens=_count(details, "cached_tokens") or _count(usage, "cache_read_input_tokens"),
            cache_write_tokens=_count(usage, "cache_creation_input_tokens") or _count(details, "cache_creation_tokens"),
            provider_cost=None if cost is None or cost < 0 else float(cost),
        )
    )
