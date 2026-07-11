"""Model pricing backed by LiteLLM's bundled model cost catalog.

This is the single source of truth for cost estimation across all
LemonCrow capabilities (tool supervision, context compression, budget
optimizer, cost tracker, HTTP dashboard).

Programmatic override:
    from lemoncrow.core.capabilities.pricing import override_pricing
    override_pricing("my-model", input_usd=1.0, output_usd=4.0)

Usage::

    from lemoncrow.core.capabilities.pricing import (
        get_model_pricing,
        tokens_to_usd,
        active_model,
    )

    model = active_model()          # from LEMONCROW_MODEL env var
    pricing = get_model_pricing(model)
    cost = tokens_to_usd(model, tokens=500, token_type="output")
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

logger = logging.getLogger(__name__)
_ACTIVE_MODEL_OVERRIDE: ContextVar[str | None] = ContextVar("lemoncrow_active_model_override", default=None)


def _intro_pricing_active(until: str) -> bool:
    """True while an ``intro`` pricing window is open (inclusive end date, UTC)."""
    try:
        return datetime.now(UTC).date() <= date.fromisoformat(until)
    except ValueError:
        return False


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
            # Time-boxed introductory rates: an optional ``intro`` block
            # (``until`` + any rate keys) replaces the sticker values while
            # the window is open; sticker applies automatically after. The
            # pricing table is cached per process, so a process spanning the
            # boundary picks up sticker rates on its next start.
            intro = rates.get("intro")
            if isinstance(intro, dict) and _intro_pricing_active(str(intro.get("until", ""))):
                rates = {**rates, **{k: v for k, v in intro.items() if k != "until"}}
            input_usd = float(rates.get("input", 0.0))
            output_usd = float(rates.get("output", 0.0))

            # Optional per-request long-context premium tier:
            #   long_context: {threshold, input, output, cache_read, cache_write}
            lc = rates.get("long_context")
            lc_threshold = int(lc.get("threshold", 200_000)) if isinstance(lc, dict) else 0

            def _lc_tier(
                name: str,
                lc: object = lc,
                lc_threshold: int = lc_threshold,
            ) -> tuple[PricingTier, ...]:
                if not isinstance(lc, dict):
                    return ()
                value = lc.get(name)
                if value is None:
                    return ()
                return (PricingTier(threshold_tokens=lc_threshold, rate=float(value)),)

            overrides[str(model_id)] = {
                "input": input_usd,
                "output": output_usd,
                "cache_read": float(rates.get("cache_read", 0.0)),
                "cache_write": float(rates.get("cache_write", 0.0)),
                "cache_write_1h": float(rates.get("cache_write_1h", 0.0)),
                "context_window": int(rates.get("context_window", 0) or 0),
                "thinking": float(rates.get("thinking", output_usd)),
                "input_tiers": _lc_tier("input"),
                "output_tiers": _lc_tier("output"),
                "cache_read_tiers": _lc_tier("cache_read"),
                "cache_write_tiers": _lc_tier("cache_write"),
                "thinking_tiers": (),
            }
    except Exception as e:
        logging.exception("Recovered from broad exception handler")
        logger.warning("Failed to load pricing overrides from %s: %s", path, e)

    return overrides


def _load_overrides_from_disk() -> dict[str, dict[str, float | tuple[PricingTier, ...]]]:
    """Load pricing overrides from repo-local and global config files.

    Checks:
    1. src/lemoncrow/core/capabilities/pricing.yaml (built-in repo config)
    2. ~/.lemoncrow/pricing.yaml (global user config)
    """
    from lemoncrow.core.foundation.paths import default_store_root

    overrides: dict[str, dict[str, float | tuple[PricingTier, ...]]] = {}

    # 1. Built-in (packaged with code)
    builtin_path = Path(__file__).parent / "pricing.yaml"
    overrides.update(_load_overrides_from_file(builtin_path))

    # 2. Global user config (~/.lemoncrow/pricing.yaml)
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


def _load_litellm_model_cost() -> dict[str, object]:
    """Load the model pricing catalog.

    Uses litellm's live ``model_cost`` dict when the package is installed
    (fresher data, includes any runtime overrides).  Falls back to the
    bundled ``model_prices.json`` snapshot so pricing works without litellm.
    """
    import importlib
    import json

    previous_litellm_log = os.environ.get("LITELLM_LOG")
    if previous_litellm_log is None:
        os.environ["LITELLM_LOG"] = "ERROR"

    try:
        litellm = importlib.import_module("litellm")
        model_cost = getattr(litellm, "model_cost", None)
        if model_cost:
            return cast(dict[str, object], model_cost)
    except Exception:  # noqa: BLE001
        pass  # fall through to bundled snapshot
    finally:
        if previous_litellm_log is None:
            os.environ.pop("LITELLM_LOG", None)
        else:
            os.environ["LITELLM_LOG"] = previous_litellm_log

    # Bundled snapshot — refreshed at release build time from the locked litellm
    # version; always available offline, no litellm import required at runtime.
    _bundle = Path(__file__).parent.parent.parent / "infra" / "model_prices.json"
    try:
        return cast(dict[str, object], json.loads(_bundle.read_text()))
    except Exception:
        logging.exception("Failed to load bundled model_prices.json")
        return {}


with_model_cost: dict[str, object] = {}  # populated lazily on first _load_pricing_table() call

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


_TOKENS_PER_MILLION = 1_000_000.0
# Anthropic prices a 1h-TTL cache write at 2x base input vs 1.25x for the 5m
# default -- i.e. 1.6x the 5m cache-write rate. Used to derive the 1h rate when a
# model card omits an explicit cache_write_1h.
_CACHE_WRITE_1H_OVER_5M = 1.6
_DATE_SUFFIX_RE = re.compile(r"(?:-\d{8}|-\d{4}-\d{2}-\d{2})(?:-v\d+:\d+)?$")
_LATEST_SUFFIX_RE = re.compile(r"-latest$")
_PREVIEW_SUFFIX_RE = re.compile(r"-preview(?:-[a-z0-9.]+)*$", re.IGNORECASE)
_ANTHROPIC_VERSION_RE = re.compile(r"^(claude-(?:opus|sonnet|haiku)-\d+)-\d+$")
_TIER_SUFFIX_RE = re.compile(r"_above_(\d+)k_tokens$")

# Vendor prefixes that denote flat-rate subscription usage rather than a real
# provider/model namespace. The tail after the slash is frequently a real,
# separately-billable model id (e.g. "copilot/gpt-5" -> "gpt-5"); generating
# that as an alias would resolve straight through to the underlying model's
# real per-token API rate and massively overbill usage that GitHub Copilot's
# flat subscription fee already covers. See copilot.py's ``copilot/<model>``
# namespacing.
# Vendors whose "<vendor>/<model>" ids identify subscription-covered usage the
# vendor never bills per-token. Exempt from alias stripping so e.g.
# "cursor/composer-2" can never resolve to a real per-token rate card
# (cursor.py._normalize_model namespaces placeholder bubbles for exactly this
# reason). OpenCode is deliberately NOT here: it's BYOK -- real keys, real
# per-token billing -- so stripping to the underlying model is correct.
_SUBSCRIPTION_VENDOR_PREFIXES = frozenset({"copilot", "cursor"})


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
        cache_write_1h: Cost per 1M 1h-TTL cache-write tokens in USD
                     (0 falls back to ``cache_write``).
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
    cache_write_1h: float = 0.0
    thinking: float = 0.0
    context_window: int = 0
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

    def cost_breakdown_usd(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_write_1h_tokens: int = 0,
        thinking_tokens: int = 0,
    ) -> dict[str, float]:
        """Compute USD cost breakdown for the given token counts.

        ``cache_write_tokens`` is the total cache-creation count; the 1h-TTL
        subset (``cache_write_1h_tokens``) is repriced at the 1h rate and folded
        into the ``cache_write`` bucket.
        """
        cw_1h = min(max(cache_write_1h_tokens, 0), cache_write_tokens)
        cw_5m = cache_write_tokens - cw_1h
        rate_1h = self.cache_write_1h or (self.cache_write * _CACHE_WRITE_1H_OVER_5M)
        return {
            "input": self._cost_for_tokens(input_tokens, self.input, self.input_tiers),
            "output": self._cost_for_tokens(output_tokens, self.output, self.output_tiers),
            "cache_read": self._cost_for_tokens(cache_read_tokens, self.cache_read, self.cache_read_tiers),
            "cache_write": self._cost_for_tokens(cw_5m, self.cache_write, self.cache_write_tiers)
            + cw_1h * rate_1h / _TOKENS_PER_MILLION,
            "thinking": self._cost_for_tokens(thinking_tokens, self.thinking or self.output, self.thinking_tiers),
        }

    def cost_usd(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_write_1h_tokens: int = 0,
        thinking_tokens: int = 0,
    ) -> float:
        """Compute total USD cost for the given token counts.

        ``cache_write_tokens`` is the *total* cache-creation count; the 1h-TTL
        subset (``cache_write_1h_tokens``) is repriced at the 1h rate (2x input
        = cache_write x 1.6 when a model card omits an explicit cache_write_1h).
        """
        cw_1h = min(max(cache_write_1h_tokens, 0), cache_write_tokens)
        cw_5m = cache_write_tokens - cw_1h
        rate_1h = self.cache_write_1h or (self.cache_write * _CACHE_WRITE_1H_OVER_5M)
        return round(
            self._cost_for_tokens(input_tokens, self.input, self.input_tiers)
            + self._cost_for_tokens(output_tokens, self.output, self.output_tiers)
            + self._cost_for_tokens(cache_read_tokens, self.cache_read, self.cache_read_tiers)
            + self._cost_for_tokens(cw_5m, self.cache_write, self.cache_write_tiers)
            + cw_1h * rate_1h / _TOKENS_PER_MILLION
            + self._cost_for_tokens(thinking_tokens, self.thinking or self.output, self.thinking_tiers),
            8,
        )

    def long_context_threshold(self) -> int:
        """Per-request long-context threshold in tokens (0 = no premium tier).

        Derived from the first tier of any token category (LiteLLM encodes the
        premium as ``*_above_200k_tokens``; YAML overrides as ``long_context``).
        """
        thresholds = [
            tiers[0].threshold_tokens
            for tiers in (self.input_tiers, self.output_tiers, self.cache_read_tiers, self.cache_write_tiers)
            if tiers
        ]
        return min(thresholds) if thresholds else 0

    def request_cost_usd(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_write_1h_tokens: int = 0,
        long_context: bool = False,
    ) -> float:
        """Per-request cost with flat rates (Anthropic billing semantics).

        Unlike :meth:`cost_usd` (progressive tiers over aggregate counts),
        Anthropic bills the *whole request* at premium rates once its context
        exceeds the long-context threshold. Callers bucket usage per request
        and pass ``long_context=True`` for the premium bucket.
        ``cache_write_tokens`` is the 5m-TTL portion; 1h-TTL writes go in
        ``cache_write_1h_tokens``.
        """

        def _premium(base: float, tiers: tuple[PricingTier, ...]) -> float:
            return tiers[0].rate if (long_context and tiers) else base

        rate_in = _premium(self.input, self.input_tiers)
        rate_out = _premium(self.output, self.output_tiers)
        rate_cr = _premium(self.cache_read, self.cache_read_tiers)
        rate_cw = _premium(self.cache_write, self.cache_write_tiers)
        base_1h = self.cache_write_1h or self.cache_write
        # 1h writes scale by the 5m write long-context multiplier. When only the
        # 1h rate is configured (no 5m base to derive the ratio from), fall back
        # to the input-side premium multiplier so long_context still applies.
        if self.cache_write > 0:
            cw_multiplier = rate_cw / self.cache_write
        elif long_context and self.input > 0:
            cw_multiplier = rate_in / self.input
        else:
            cw_multiplier = 1.0
        rate_cw1 = base_1h * cw_multiplier
        return round(
            (
                input_tokens * rate_in
                + output_tokens * rate_out
                + cache_read_tokens * rate_cr
                + cache_write_tokens * rate_cw
                + cache_write_1h_tokens * rate_cw1
            )
            / _TOKENS_PER_MILLION,
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


def _int_or_zero(value: object) -> int:
    """Coerce a catalog value to int, tolerating junk (LiteLLM's sample_spec)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


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
        "cache_write_1h": _rate("cache_creation_input_token_cost_above_1hr"),
        "context_window": _int_or_zero(raw_entry.get("max_input_tokens")),
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
        vendor, tail = model_id.split("/", 1)
        if vendor not in _SUBSCRIPTION_VENDOR_PREFIXES:
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
    LiteLLM is the sole pricing source; no LemonCrow-specific defaults are added.
    """
    table: dict[str, dict[str, float | tuple[PricingTier, ...]]] = {
        "_default": {
            "input": 0.0,
            "output": 0.0,
            "cache_read": 0.0,
            "cache_write": 0.0,
            "cache_write_1h": 0.0,
            "context_window": 0,
            "thinking": 0.0,
            "input_tiers": (),
            "output_tiers": (),
            "cache_read_tiers": (),
            "cache_write_tiers": (),
            "thinking_tiers": (),
        }
    }
    priorities: dict[str, tuple[int, int, int, tuple[int, ...], int]] = {"_default": (0, 0, 0, (), 0)}

    prices = _load_litellm_model_cost()
    for model_id, raw_entry in prices.items():
        pricing_entry = _extract_pricing_entry(model_id, raw_entry)
        if pricing_entry is None:
            continue
        _register_entry(table, priorities, model_id, pricing_entry)
        # Subscription-tier entries (GitHub Copilot, etc.) have zero per-token
        # pricing and no input_cost_per_token field.  Their provider-stripped
        # alias ("github_copilot/claude-sonnet-4.6" → "claude-sonnet-4.6")
        # would shadow real Anthropic entries when litellm is not installed.
        # Only generate the bare-name provider alias for entries with real pricing.
        _has_token_price = bool(isinstance(raw_entry, dict) and raw_entry.get("input_cost_per_token")) or bool(
            isinstance(raw_entry, dict) and raw_entry.get("output_cost_per_token")
        )
        for alias in _alias_candidates(model_id):
            _slash_alias = "/" in model_id and alias == model_id.split("/", 1)[1]
            if _slash_alias and not _has_token_price:
                continue
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


# Last-resort static fallback rates (Sonnet-class, USD per million tokens) for
# callers that must show a non-zero estimate even when the pricing table lookup
# itself fails. Single source for the previously duplicated "$3 in / $15 out"
# constants (savings_summary.estimate_cost_usd, reporting/dashboard,
# model_routing/cache_cost).
FALLBACK_INPUT_USD_PER_MTOK = 3.0
FALLBACK_OUTPUT_USD_PER_MTOK = 15.0
FALLBACK_CACHE_READ_USD_PER_MTOK = 0.30
FALLBACK_CACHE_WRITE_USD_PER_MTOK = 3.75


def fallback_cost_usd(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Price a request at the static Sonnet-class fallback rates."""
    return (
        max(0, input_tokens) * FALLBACK_INPUT_USD_PER_MTOK
        + max(0, output_tokens) * FALLBACK_OUTPUT_USD_PER_MTOK
        + max(0, cache_read_tokens) * FALLBACK_CACHE_READ_USD_PER_MTOK
        + max(0, cache_write_tokens) * FALLBACK_CACHE_WRITE_USD_PER_MTOK
    ) / 1_000_000


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

    Lookup order (LiteLLM is the sole pricing source — no LemonCrow overrides):
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

    # 2. Alias candidates on the raw id (strips vendor prefixes, e.g. "openai/"
    #    -- but NOT subscription-only prefixes like "copilot/", see
    #    _SUBSCRIPTION_VENDOR_PREFIXES)
    for alias in _alias_candidates(model_id):
        if hit := _lookup(alias):
            return hit

    # 3. Dot-version normalisation ("claude-sonnet-4.6" → "claude-sonnet-4-6")
    normalised = _normalize_model_id(model_id)
    if normalised != model_id and (hit := _lookup(normalised)):
        return hit

    # 4. Alias candidates on the normalised id (strips date / preview suffixes)
    for alias in _alias_candidates(normalised):
        if hit := _lookup(alias):
            return hit

    # 4. Unknown — log once, return zero-cost sentinel
    if model_id and model_id not in _PLACEHOLDER_MODEL_IDS and model_id not in _warned_unknown_models:
        _warned_unknown_models.add(model_id)
        logger.debug(
            "lemoncrow.pricing: no entry for model %r — costs for this model will be reported as $0. "
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
    cache_write_1h_tokens: int = 0,
    thinking_tokens: int = 0,
) -> float:
    """Compute usage cost for a model via the shared pricing catalog."""
    return get_model_pricing(model_id).cost_usd(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_write_1h_tokens=cache_write_1h_tokens,
        thinking_tokens=thinking_tokens,
    )


def usage_cost_breakdown_usd(
    model_id: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    thinking_tokens: int = 0,
) -> dict[str, float]:
    """Compute usage cost breakdown for a model via the shared pricing catalog."""
    return get_model_pricing(model_id).cost_breakdown_usd(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_write_1h_tokens=cache_write_1h_tokens,
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

    Reads ``LEMONCROW_MODEL`` (set by the agent runtime or user config).
    Falls back to ``"_default"`` so ``get_model_pricing`` still works.
    """
    override = _ACTIVE_MODEL_OVERRIDE.get()
    if override:
        return override
    return os.environ.get("LEMONCROW_MODEL", "_default")


@contextlib.contextmanager
def active_model_override(model_id: str | None) -> Iterator[None]:
    """Temporarily override ``active_model()`` for the current execution context."""

    if not model_id:
        yield
        return
    token = _ACTIVE_MODEL_OVERRIDE.set(model_id)
    try:
        yield
    finally:
        _ACTIVE_MODEL_OVERRIDE.reset(token)


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
