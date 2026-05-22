"""Vendor price tables and CandidateModel definitions.

See docs/plans/active/commercial-wedge/W2-counterfactual.md for the full spec.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    """Per-million-token prices for a model."""

    input: float   # USD per million input tokens
    output: float  # USD per million output tokens

    def cost_usd(self, *, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input + output_tokens * self.output) / 1_000_000


@dataclass(frozen=True)
class CandidateModel:
    """A vendor+model pair with pricing and capability metadata."""

    vendor: str
    model_id: str
    tier: str  # "cheap" | "high"
    pricing: ModelPricing
    supports_tool_use: bool = True
    output_multiplier: float = 1.0
    context_window: int = 200_000


@dataclass(frozen=True)
class PricingTable:
    """Versioned collection of CandidateModel entries."""

    version: str
    candidates: tuple[CandidateModel, ...]

    def candidates_for_vendor(self, vendor: str) -> tuple[CandidateModel, ...]:
        return tuple(c for c in self.candidates if c.vendor == vendor)


# ---------------------------------------------------------------------------
# Bundled default pricing table (version-stamped)
# ---------------------------------------------------------------------------

_DEFAULT_CANDIDATES: tuple[CandidateModel, ...] = (
    # Anthropic
    CandidateModel(
        vendor="anthropic",
        model_id="claude-haiku-4-5",
        tier="cheap",
        pricing=ModelPricing(input=0.80, output=4.00),
        supports_tool_use=True,
    ),
    CandidateModel(
        vendor="anthropic",
        model_id="claude-sonnet-4-5",
        tier="high",
        pricing=ModelPricing(input=3.00, output=15.00),
        supports_tool_use=True,
    ),
    CandidateModel(
        vendor="anthropic",
        model_id="claude-opus-4-5",
        tier="high",
        pricing=ModelPricing(input=15.00, output=75.00),
        supports_tool_use=True,
        output_multiplier=1.5,
    ),
    # OpenAI
    CandidateModel(
        vendor="openai",
        model_id="gpt-4o-mini",
        tier="cheap",
        pricing=ModelPricing(input=0.15, output=0.60),
        supports_tool_use=True,
    ),
    CandidateModel(
        vendor="openai",
        model_id="gpt-4o",
        tier="high",
        pricing=ModelPricing(input=2.50, output=10.00),
        supports_tool_use=True,
    ),
    # Google
    CandidateModel(
        vendor="google",
        model_id="gemini-2.0-flash",
        tier="cheap",
        pricing=ModelPricing(input=0.10, output=0.40),
        supports_tool_use=True,
    ),
    CandidateModel(
        vendor="google",
        model_id="gemini-2.0-pro",
        tier="high",
        pricing=ModelPricing(input=1.25, output=5.00),
        supports_tool_use=True,
    ),
)

_DEFAULT_TABLE = PricingTable(version="2026-05-20", candidates=_DEFAULT_CANDIDATES)


def load_pricing_table(_version: str | None = None) -> PricingTable:
    """Load the bundled pricing table (version pin is a no-op until W2 is fully implemented)."""
    return _DEFAULT_TABLE


__all__ = ["CandidateModel", "ModelPricing", "PricingTable", "load_pricing_table"]
