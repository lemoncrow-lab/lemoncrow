"""Model pricing loader — maps model IDs to USD-per-1M-token rates.

This is the single source of truth for cost estimation across all
Atelier capabilities (tool supervision, context compression, budget
optimizer, cost tracker, HTTP dashboard).

Config resolution order (first found wins):
1. ``src/atelier/model_pricing.toml`` bundled with the package.

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

import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Default bundled config path
# ---------------------------------------------------------------------------

_BUNDLED_TOML = Path(__file__).resolve().parent.parent.parent / "model_pricing.toml"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPricing:
    """USD per 1 Million tokens for a specific model.

    Attributes:
        model_id:    Canonical model identifier (may be ``"_default"``).
        input:       Cost per 1M input (prompt) tokens in USD.
        output:      Cost per 1M output (completion) tokens in USD.
        cache_read:  Cost per 1M cache-read tokens in USD (0 if not applicable).
        cache_write: Cost per 1M cache-write tokens in USD (0 if not applicable).
        known:       ``True`` when pricing was explicitly configured for this
                     model; ``False`` when the model id was not found and the
                     default (all zeros) was used instead.
    """

    model_id: str
    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0
    known: bool = True

    def cost_usd(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """Compute total USD cost for the given token counts."""
        # Note: if cache_write is not specified in TOML, we default to 0.0.
        # However, Anthropic typically bills cache_write at 1.25x input.
        cw_rate = (
            self.cache_write
            if self.cache_write > 0
            else (self.input * 1.25 if "claude" in self.model_id.lower() else self.input)
        )

        return round(
            (
                input_tokens * self.input
                + output_tokens * self.output
                + cache_read_tokens * self.cache_read
                + cache_write_tokens * cw_rate
            )
            / 1_000_000.0,
            8,
        )

    def tokens_to_usd(
        self,
        tokens: int,
        token_type: Literal["input", "output", "cache_read", "cache_write"] = "output",
    ) -> float:
        """Convert a single token count to USD cost."""
        if token_type == "cache_write":
            rate = (
                self.cache_write
                if self.cache_write > 0
                else (self.input * 1.25 if "claude" in self.model_id.lower() else self.input)
            )
        else:
            rate = {"input": self.input, "output": self.output, "cache_read": self.cache_read}[token_type]
        return round(tokens * rate / 1_000_000.0, 8)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def _resolve_config_path() -> Path:
    return _BUNDLED_TOML


@lru_cache(maxsize=1)
def _load_pricing_table() -> dict[str, dict[str, float]]:
    """Load and cache the TOML pricing table.

    Returns a flat dict: ``{model_id: {"input": float, "output": float,
    "cache_read": float}}``.  Always includes ``"_default"``.
    """
    path = _resolve_config_path()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}

    table: dict[str, dict[str, float]] = {}

    # Parse [models.<id>] sections
    for model_id, vals in raw.get("models", {}).items():
        if isinstance(vals, dict):
            table[model_id] = {
                "input": float(vals.get("input", 3.0)),
                "output": float(vals.get("output", 15.0)),
                "cache_read": float(vals.get("cache_read", 0.0)),
                "cache_write": float(vals.get("cache_write", 0.0)),
            }

    # Parse [default] section
    default_vals = raw.get("default", {})
    table["_default"] = {
        "input": float(default_vals.get("input", 3.0)),
        "output": float(default_vals.get("output", 15.0)),
        "cache_read": float(default_vals.get("cache_read", 0.30)),
        "cache_write": float(default_vals.get("cache_write", 0.0)),
    }

    return table


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_model_pricing(model_id: str) -> ModelPricing:
    """Return :class:`ModelPricing` for *model_id*.

    When the model id is not found in the pricing table (exact or prefix
    match), the default (all-zeros) entry is returned with ``known=False``.
    Matching is exact first, then prefix (so ``"claude-sonnet-4"`` matches
    ``"claude-sonnet-4-5"`` entries with a ``startswith`` check).
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
    # Fallback to default (pricing not configured → known=False)
    vals = table["_default"]
    return ModelPricing(model_id=model_id, known=False, **vals)


def tokens_to_usd(
    model_id: str,
    tokens: int,
    token_type: Literal["input", "output", "cache_read"] = "output",
) -> float:
    """Convenience: convert *tokens* to USD for *model_id*.

    >>> tokens_to_usd("claude-sonnet-4", 1_000_000, "output")
    15.0
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
) -> None:
    """Programmatically add or update an entry in the in-memory pricing table.

    Changes are not written back to the TOML file.  Useful for tests or
    runtime overrides from a control plane.

    Args:
        model_id:        Model identifier to register/overwrite.
        input_usd:       USD per 1M input tokens.
        output_usd:      USD per 1M output tokens.
        cache_read_usd:  USD per 1M cache-read tokens.
    """
    _load_pricing_table()  # ensure cache is populated
    table = _load_pricing_table.__wrapped__()
    table[model_id] = {
        "input": input_usd,
        "output": output_usd,
        "cache_read": cache_read_usd,
    }
    # Invalidate the cache so next call re-reads (via the now-updated table)
    _load_pricing_table.cache_clear()


def all_known_models() -> list[str]:
    """Return every model ID known to the pricing table (excluding ``_default``)."""
    return [k for k in _load_pricing_table() if k != "_default"]
