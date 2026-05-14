"""Model pricing backed by LiteLLM's bundled model cost catalog.

This is the single source of truth for cost estimation across all
Atelier capabilities (tool supervision, context compression, budget
optimizer, cost tracker, HTTP dashboard).

Programmatic override:
    from atelier.core.capabilities.pricing import override_pricing
    override_pricing("my-model", input_usd=1.0, output_usd=4.0)

Usage::

    from atelier.core.capabilities.pricing import (
        get_model_pricing,
        tokens_to_usd,
        active_model,
    )

    model = active_model()          # from ATELIER_MODEL env var
    pricing = get_model_pricing(model)
    cost = tokens_to_usd(model, tokens=500, token_type="output")
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

logger = logging.getLogger(__name__)

# Placeholder model ids that legitimately carry no cost. Returning the zero-cost
# default for these is correct and should not log a warning.
#   "<synthetic>" — Anthropic placeholder for cached/instant assistant replies
#                   (e.g. Claude Code injects this for synthesised tool follow-ups
#                   that don't trigger a billable /v1/messages request).
#   "_default"    — explicit sentinel used by the pricing table itself.
#   ""            — missing/unknown at the call site.
_PLACEHOLDER_MODEL_IDS = frozenset({"", "_default", "<synthetic>"})

_warned_unknown_models: set[str] = set()

with_model_cost: dict[str, object]
try:
    from litellm import model_cost as with_model_cost
except Exception:
    with_model_cost = {}

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


_TOKENS_PER_MILLION = 1_000_000.0
_DATE_SUFFIX_RE = re.compile(r"(?:-\d{8}|-\d{4}-\d{2}-\d{2})(?:-v\d+:\d+)?$")
_LATEST_SUFFIX_RE = re.compile(r"-latest$")
_PREVIEW_SUFFIX_RE = re.compile(r"-preview(?:-[a-z0-9.]+)*$", re.IGNORECASE)
_ANTHROPIC_VERSION_RE = re.compile(r"^(claude-(?:opus|sonnet|haiku)-\d+)-\d+$")
_TIER_SUFFIX_RE = re.compile(r"_above_(\d+)k_tokens$")


@dataclass(frozen=True)
class PricingTier:
    threshold_tokens: int
    rate: float


@dataclass(frozen=True)
class ModelPricing:
    """USD per 1 Million tokens for a specific model.

    Attributes:
        model_id:    Canonical model identifier (may be ``"_default"``).
        input:       Cost per 1M input (prompt) tokens in USD.
        output:      Cost per 1M output (completion) tokens in USD.
        cache_read:  Cost per 1M cache-read tokens in USD (0 if not applicable).
        cache_write: Cost per 1M cache-write tokens in USD (0 if not applicable).
        thinking:    Cost per 1M reasoning/thinking tokens in USD.
        known:       ``True`` when pricing was explicitly configured for this
                     model; ``False`` when the model id was not found and the
                     zero-cost default was used instead.
    """

    model_id: str
    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0
    thinking: float = 0.0
    input_tiers: tuple[PricingTier, ...] = ()
    output_tiers: tuple[PricingTier, ...] = ()
    cache_read_tiers: tuple[PricingTier, ...] = ()
    cache_write_tiers: tuple[PricingTier, ...] = ()
    thinking_tiers: tuple[PricingTier, ...] = ()
    known: bool = True

    @staticmethod
    def _cost_for_tokens(tokens: int, base_rate: float, tiers: tuple[PricingTier, ...]) -> float:
        if tokens <= 0 or base_rate <= 0:
            return 0.0

        total = 0.0
        billed = 0
        rate = base_rate
        for tier in tiers:
            if tokens <= billed:
                break
            chunk = min(tokens, tier.threshold_tokens) - billed
            if chunk > 0:
                total += chunk * rate / _TOKENS_PER_MILLION
                billed += chunk
            rate = tier.rate

        if tokens > billed:
            total += (tokens - billed) * rate / _TOKENS_PER_MILLION

        return total

    def cost_usd(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        thinking_tokens: int = 0,
    ) -> float:
        """Compute total USD cost for the given token counts."""
        return round(
            self._cost_for_tokens(input_tokens, self.input, self.input_tiers)
            + self._cost_for_tokens(output_tokens, self.output, self.output_tiers)
            + self._cost_for_tokens(cache_read_tokens, self.cache_read, self.cache_read_tiers)
            + self._cost_for_tokens(cache_write_tokens, self.cache_write, self.cache_write_tiers)
            + self._cost_for_tokens(thinking_tokens, self.thinking or self.output, self.thinking_tiers),
            8,
        )

    def tokens_to_usd(
        self,
        tokens: int,
        token_type: Literal["input", "output", "cache_read", "cache_write", "thinking"] = "output",
    ) -> float:
        """Convert a single token count to USD cost."""
        tiers = {
            "input": self.input_tiers,
            "output": self.output_tiers,
            "cache_read": self.cache_read_tiers,
            "cache_write": self.cache_write_tiers,
            "thinking": self.thinking_tiers,
        }[token_type]
        rate = {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "thinking": self.thinking or self.output,
        }[token_type]
        return round(self._cost_for_tokens(tokens, rate, tiers), 8)


# ---------------------------------------------------------------------------
# LiteLLM catalog loader
# ---------------------------------------------------------------------------


_OVERRIDE_PRICING: dict[str, dict[str, float | tuple[PricingTier, ...]]] = {}


def _extract_tiers(entry: dict[str, object], prefix: str) -> tuple[PricingTier, ...]:
    tiers: list[PricingTier] = []
    needle = f"{prefix}_above_"
    for key, value in entry.items():
        if not key.startswith(needle):
            continue
        match = _TIER_SUFFIX_RE.search(key)
        if not match:
            continue
        threshold_tokens = int(match.group(1)) * 1000
        try:
            rate = float(value or 0.0) * _TOKENS_PER_MILLION
        except (TypeError, ValueError):
            continue
        if rate <= 0:
            continue
        tiers.append(PricingTier(threshold_tokens=threshold_tokens, rate=rate))
    return tuple(sorted(tiers, key=lambda tier: tier.threshold_tokens))


def _extract_pricing_entry(model_id: str, raw_entry: object) -> dict[str, float | tuple[PricingTier, ...]] | None:
    if not isinstance(raw_entry, dict):
        return None

    def _rate(name: str) -> float:
        try:
            return float(raw_entry.get(name) or 0.0) * _TOKENS_PER_MILLION
        except (TypeError, ValueError):
            return 0.0

    return {
        "input": _rate("input_cost_per_token"),
        "output": _rate("output_cost_per_token"),
        "cache_read": _rate("cache_read_input_token_cost"),
        "cache_write": _rate("cache_creation_input_token_cost"),
        "thinking": _rate("output_cost_per_reasoning_token") or _rate("output_cost_per_token"),
        "input_tiers": _extract_tiers(raw_entry, "input_cost_per_token"),
        "output_tiers": _extract_tiers(raw_entry, "output_cost_per_token"),
        "cache_read_tiers": _extract_tiers(raw_entry, "cache_read_input_token_cost"),
        "cache_write_tiers": _extract_tiers(raw_entry, "cache_creation_input_token_cost"),
        "thinking_tiers": _extract_tiers(raw_entry, "output_cost_per_reasoning_token")
        or _extract_tiers(raw_entry, "output_cost_per_token"),
    }


def _alias_candidates(model_id: str) -> set[str]:
    aliases: set[str] = set()

    def _add(alias: str) -> None:
        alias = alias.strip()
        if alias and alias != model_id:
            aliases.add(alias)

    without_latest = _LATEST_SUFFIX_RE.sub("", model_id)
    _add(without_latest)

    without_date = _DATE_SUFFIX_RE.sub("", model_id)
    _add(without_date)
    if without_date != model_id:
        _add(_LATEST_SUFFIX_RE.sub("", without_date))

    without_preview = _PREVIEW_SUFFIX_RE.sub("", model_id)
    _add(without_preview)
    if without_preview != model_id:
        _add(_DATE_SUFFIX_RE.sub("", without_preview))

    anthropic_match = _ANTHROPIC_VERSION_RE.match(model_id)
    if anthropic_match:
        _add(anthropic_match.group(1))

    if "/" in model_id and model_id.count("/") == 1:
        _, tail = model_id.split("/", 1)
        _add(tail)

    return aliases


def _alias_priority(model_id: str) -> tuple[int, int, int, tuple[int, ...], int]:
    normalized = _DATE_SUFFIX_RE.sub("", model_id)
    version_parts = tuple(-int(part) for part in re.findall(r"\d+", normalized))
    return (
        1 if "preview" in model_id.lower() else 0,
        1 if model_id.endswith("-latest") else 0,
        1 if _DATE_SUFFIX_RE.search(model_id) else 0,
        version_parts,
        len(model_id),
    )


def _register_entry(
    table: dict[str, dict[str, float | tuple[PricingTier, ...]]],
    priorities: dict[str, tuple[int, int, int, tuple[int, ...], int]],
    key: str,
    value: dict[str, float | tuple[PricingTier, ...]],
    *,
    priority: tuple[int, int, int, tuple[int, ...], int] | None = None,
) -> None:
    entry_priority = priority or _alias_priority(key)
    current = priorities.get(key)
    if current is None or entry_priority < current:
        table[key] = value
        priorities[key] = entry_priority


@lru_cache(maxsize=1)
def _load_pricing_table() -> dict[str, dict[str, float | tuple[PricingTier, ...]]]:
    """Load and cache the LiteLLM pricing table.

    Returns a flat dict: ``{model_id: {"input": float, "output": float,
    "cache_read": float, ...}}``. Always includes ``"_default"``.
    """
    table: dict[str, dict[str, float | tuple[PricingTier, ...]]] = {
        "_default": {
            "input": 0.0,
            "output": 0.0,
            "cache_read": 0.0,
            "cache_write": 0.0,
            "thinking": 0.0,
            "input_tiers": (),
            "output_tiers": (),
            "cache_read_tiers": (),
            "cache_write_tiers": (),
            "thinking_tiers": (),
        }
    }
    priorities: dict[str, tuple[int, int, int, tuple[int, ...], int]] = {"_default": (0, 0, 0, (), 0)}

    for model_id, raw_entry in with_model_cost.items():
        pricing_entry = _extract_pricing_entry(model_id, raw_entry)
        if pricing_entry is None:
            continue
        _register_entry(table, priorities, model_id, pricing_entry)
        for alias in _alias_candidates(model_id):
            _register_entry(table, priorities, alias, pricing_entry, priority=_alias_priority(model_id))

    # Built-in zero-cost entries for host-internal model aliases that don't
    # correspond to a real billable API. Without these, get_model_pricing would
    # emit a "no entry for model" warning every time the operator imports a
    # session that used one of these. We treat them as known-but-free so the
    # warning channel stays useful for genuinely missing models.
    #
    # opencode/big-pickle:  opencode's internal routing alias; the opencode
    #                       binary itself reports cost=0 in its own logs.
    # copilot/<anything>:   GitHub Copilot chat models are subscription-covered
    #                       ($19/mo Pro), not per-token billed. Importers
    #                       prefix VSCode Copilot Chat calls with ``copilot/``
    #                       so they all match this zero-cost wildcard.
    _ZERO_COST = {
        "input": 0.0,
        "output": 0.0,
        "cache_read": 0.0,
        "cache_write": 0.0,
        "thinking": 0.0,
        "input_tiers": (),
        "output_tiers": (),
        "cache_read_tiers": (),
        "cache_write_tiers": (),
        "thinking_tiers": (),
    }
    for alias in ("opencode/big-pickle", "copilot/"):
        if alias not in table:
            table[alias] = dict(_ZERO_COST)

    for model_id, entry in _OVERRIDE_PRICING.items():
        table[model_id] = entry

    return table


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_placeholder_model(model_id: str | None) -> bool:
    """Return True when *model_id* is a known placeholder (no real billing).

    Useful so callers (parsers, summarizers) can avoid surfacing things like
    ``<synthetic>`` as the trace's resolved model.
    """
    return str(model_id or "").strip() in _PLACEHOLDER_MODEL_IDS


def get_model_pricing(model_id: str) -> ModelPricing:
    """Return :class:`ModelPricing` for *model_id*.

    When the model id is not found in the pricing table (exact or prefix
    match), the default (all-zeros) entry is returned with ``known=False``.
    Matching is exact first, then prefix over the LiteLLM-backed catalog.

    Placeholder ids (``<synthetic>``, ``_default``, empty) are returned as
    zero-cost silently. Genuinely unknown model ids log a one-time warning so
    the operator can extend ``_OVERRIDE_PRICING`` or upgrade LiteLLM.
    """
    table = _load_pricing_table()
    # Exact match
    if model_id in table:
        vals = table[model_id]
        return ModelPricing(model_id=model_id, known=True, **vals)
    # Prefix match (e.g. "claude-sonnet" → first entry starting with "claude-sonnet")
    for key, vals in table.items():
        if key != "_default" and (model_id.startswith(key) or key.startswith(model_id)):
            return ModelPricing(model_id=key, known=True, **vals)
    # Fallback to zero-cost default (pricing not configured → known=False)
    if model_id and model_id not in _PLACEHOLDER_MODEL_IDS and model_id not in _warned_unknown_models:
        _warned_unknown_models.add(model_id)
        logger.warning(
            "atelier.pricing: no entry for model %r — costs for this model will be reported as $0. "
            "Extend the LiteLLM catalog or call override_pricing() to fix.",
            model_id,
        )
    vals = table["_default"]
    return ModelPricing(model_id=model_id, known=False, **vals)


def usage_cost_usd(
    model_id: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    thinking_tokens: int = 0,
) -> float:
    """Compute usage cost for a model via the shared pricing catalog."""
    return get_model_pricing(model_id).cost_usd(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        thinking_tokens=thinking_tokens,
    )


def tokens_to_usd(
    model_id: str,
    tokens: int,
    token_type: Literal["input", "output", "cache_read", "cache_write", "thinking"] = "output",
) -> float:
    """Convenience: convert *tokens* to USD for *model_id*.

    Tiered models use LiteLLM's threshold pricing, so large token counts can
    cost more than ``tokens * base_rate`` once a threshold is crossed.
    """
    return get_model_pricing(model_id).tokens_to_usd(tokens, token_type)


def active_model() -> str:
    """Return the currently configured model from the environment.

    Reads ``ATELIER_MODEL`` (set by the agent runtime or user config).
    Falls back to ``"_default"`` so ``get_model_pricing`` still works.
    """
    return os.environ.get("ATELIER_MODEL", "_default")


def override_pricing(
    model_id: str,
    *,
    input_usd: float,
    output_usd: float,
    cache_read_usd: float = 0.0,
    cache_write_usd: float = 0.0,
    thinking_usd: float | None = None,
) -> None:
    """Programmatically add or update an entry in the in-memory pricing table.

    Changes are not written back to LiteLLM. Useful for tests or runtime
    overrides from a control plane.

    Args:
        model_id:        Model identifier to register/overwrite.
        input_usd:       USD per 1M input tokens.
        output_usd:      USD per 1M output tokens.
        cache_read_usd:  USD per 1M cache-read tokens.
        cache_write_usd: USD per 1M cache-write tokens.
        thinking_usd:    USD per 1M reasoning/thinking tokens.
    """
    _OVERRIDE_PRICING[model_id] = {
        "input": input_usd,
        "output": output_usd,
        "cache_read": cache_read_usd,
        "cache_write": cache_write_usd,
        "thinking": output_usd if thinking_usd is None else thinking_usd,
        "input_tiers": (),
        "output_tiers": (),
        "cache_read_tiers": (),
        "cache_write_tiers": (),
        "thinking_tiers": (),
    }
    _load_pricing_table.cache_clear()


def all_known_models() -> list[str]:
    """Return every model ID known to the pricing table (excluding ``_default``)."""
    return sorted(k for k in _load_pricing_table() if k != "_default")
