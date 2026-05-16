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
from pathlib import Path
from typing import Any, Literal, cast

logger = logging.getLogger(__name__)


def _load_overrides_from_file(path: Path) -> dict[str, dict[str, float | tuple[PricingTier, ...]]]:
    """Helper to load overrides from a specific YAML path."""
    try:
        import yaml
    except ImportError:
        return {}

    if not path.exists():
        return {}

    overrides: dict[str, dict[str, float | tuple[PricingTier, ...]]] = {}
    try:
        content = path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            return {}

        raw_overrides = data.get("overrides", data)
        if not isinstance(raw_overrides, dict):
            return {}

        for model_id, rates in raw_overrides.items():
            if not isinstance(rates, dict):
                continue
            input_usd = float(rates.get("input", 0.0))
            output_usd = float(rates.get("output", 0.0))
            overrides[str(model_id)] = {
                "input": input_usd,
                "output": output_usd,
                "cache_read": float(rates.get("cache_read", 0.0)),
                "cache_write": float(rates.get("cache_write", 0.0)),
                "thinking": float(rates.get("thinking", output_usd)),
                "input_tiers": (),
                "output_tiers": (),
                "cache_read_tiers": (),
                "cache_write_tiers": (),
                "thinking_tiers": (),
            }
    except Exception as e:
        logger.warning("Failed to load pricing overrides from %s: %s", path, e)

    return overrides


def _load_overrides_from_disk() -> dict[str, dict[str, float | tuple[PricingTier, ...]]]:
    """Load pricing overrides from repo-local and global config files.

    Checks:
    1. src/atelier/core/capabilities/pricing.yaml (built-in repo config)
    2. ~/.atelier/pricing.yaml (global user config)
    """
    from atelier.core.foundation.paths import default_store_root

    overrides: dict[str, dict[str, float | tuple[PricingTier, ...]]] = {}

    # 1. Built-in (packaged with code)
    builtin_path = Path(__file__).parent / "pricing.yaml"
    overrides.update(_load_overrides_from_file(builtin_path))

    # 2. Global user config (~/.atelier/pricing.yaml)
    global_path = default_store_root() / "pricing.yaml"
    overrides.update(_load_overrides_from_file(global_path))

    return overrides


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
        if value is None:
            continue
        if not isinstance(value, (int, float, str)):
            continue
        try:
            rate = float(value) * _TOKENS_PER_MILLION
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
        val = raw_entry.get(name)
        if isinstance(val, (int, float, str)):
            try:
                return float(val) * _TOKENS_PER_MILLION
            except (TypeError, ValueError):
                return 0.0
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
    LiteLLM is the sole pricing source; no Atelier-specific defaults are added.
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

    # Apply disk-based overrides from built-in and global config
    for model_id, entry in _load_overrides_from_disk().items():
        table[model_id] = entry

    # Apply programmatic overrides (highest precedence)
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


_DOT_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


def _normalize_model_id(model_id: str) -> str:
    """Normalise dot-separated version numbers to dashes.

    Anthropic's API returns model IDs using either convention depending on
    when the model was released — e.g. ``claude-sonnet-4.6`` (dot) vs the
    older ``claude-sonnet-4-5`` (dash).  LiteLLM catalogues only the dash
    form, so we normalise before lookup.

    Examples::

        "claude-sonnet-4.6"  →  "claude-sonnet-4-6"
        "claude-opus-4.7"    →  "claude-opus-4-7"
        "gpt-4o"             →  "gpt-4o"   (unchanged — no dot-version)
    """
    return _DOT_VERSION_RE.sub(r"\1-\2", model_id)


def get_model_pricing(model_id: str) -> ModelPricing:
    """Return :class:`ModelPricing` for *model_id*.

    Lookup order (LiteLLM is the sole pricing source — no Atelier overrides):
    1. Exact match against the LiteLLM-backed table.
    2. Dot-to-dash version normalisation (``claude-sonnet-4.6`` → ``claude-sonnet-4-6``).
    3. Alias stripping via :func:`_alias_candidates` on the normalised id
       (removes date/preview suffixes so ``claude-opus-4-7-20260416`` → ``claude-opus-4-7``).
    4. Zero-cost default with ``known=False`` — a one-time warning is logged so
       the operator knows to upgrade LiteLLM.

    Placeholder ids (``<synthetic>``, ``_default``, empty) are returned as
    zero-cost silently.
    """
    table = _load_pricing_table()

    def _lookup(mid: str) -> ModelPricing | None:
        if mid in table:
            entry = cast(dict[str, Any], table[mid])
            return ModelPricing(model_id=mid, known=True, **entry)
        return None

    # 1. Exact match
    if hit := _lookup(model_id):
        return hit

    # 2. Alias candidates on the raw id (strips "copilot/" prefix etc.)
    for alias in _alias_candidates(model_id):
        if hit := _lookup(alias):
            return hit

    # 3. Dot-version normalisation ("claude-sonnet-4.6" → "claude-sonnet-4-6")
    normalised = _normalize_model_id(model_id)
    if normalised != model_id:
        if hit := _lookup(normalised):
            return hit

    # 4. Alias candidates on the normalised id (strips date / preview suffixes)
    for alias in _alias_candidates(normalised):
        if hit := _lookup(alias):
            return hit

    # 4. Unknown — log once, return zero-cost sentinel
    if model_id and model_id not in _PLACEHOLDER_MODEL_IDS and model_id not in _warned_unknown_models:
        _warned_unknown_models.add(model_id)
        logger.warning(
            "atelier.pricing: no entry for model %r — costs for this model will be reported as $0. "
            "Upgrade LiteLLM or call override_pricing() to fix.",
            model_id,
        )
    entry = cast(dict[str, Any], table["_default"])
    return ModelPricing(model_id=model_id, known=False, **entry)


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
