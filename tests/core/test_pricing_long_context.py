"""Long-context pricing matrix: which models bill the full 1M window at standard rates.

Fable 5, Mythos 5, Mythos Preview, Opus 4.8/4.7/4.6, Sonnet 5, and Sonnet 4.6
include the full 1M-token context window at standard per-token pricing — a
900k-token request bills at the same per-token rate as a 9k one — so their
pricing entries must never carry >200k premium tiers. Only the pre-4.6 1M-beta
models (Sonnet 4.5 / Sonnet 4) reprice the whole request at premium rates past
200k. The standard-pricing models are pinned as full-entry overrides in
pricing.yaml, so litellm catalog drift cannot reintroduce premium tiers.
"""

from __future__ import annotations

import pytest

from lemoncrow.core.capabilities.pricing import _intro_pricing_active, get_model_pricing
from lemoncrow.core.capabilities.savings_summary import resolve_model_id

# Sonnet 5 bills launch-promo rates ($2/$10 + standard cache multipliers)
# through 2026-08-31, sticker ($3/$15) after.
_SONNET_5_RATES = (2.0, 10.0, 0.2, 2.5) if _intro_pricing_active("2026-08-31") else (3.0, 15.0, 0.3, 3.75)

# model -> (input, output, cache_read, cache_write) USD per 1M tokens
_FULL_WINDOW_STANDARD: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5": (10.0, 50.0, 1.0, 12.5),
    "claude-mythos-5": (10.0, 50.0, 1.0, 12.5),
    "claude-mythos-preview": (10.0, 50.0, 1.0, 12.5),
    "claude-opus-4-8": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-7": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-6": (5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-5": _SONNET_5_RATES,
    "claude-sonnet-4-6": (3.0, 15.0, 0.3, 3.75),
}


@pytest.mark.parametrize(("model", "rates"), sorted(_FULL_WINDOW_STANDARD.items()))
def test_full_window_models_have_no_long_context_premium(model: str, rates: tuple[float, float, float, float]) -> None:
    pricing = get_model_pricing(model)
    assert pricing is not None and pricing.known, f"{model} missing from pricing table"
    assert (pricing.input, pricing.output, pricing.cache_read, pricing.cache_write) == rates
    assert pricing.long_context_threshold() == 0
    assert pricing.input_tiers == () and pricing.output_tiers == ()
    assert pricing.cache_read_tiers == () and pricing.cache_write_tiers == ()
    # A 900k-token request bills at the same per-token rate as a 9k one.
    assert pricing.request_cost_usd(input_tokens=900_000, long_context=True) == pytest.approx(
        100 * pricing.request_cost_usd(input_tokens=9_000)
    )


def test_sonnet_4_5_keeps_200k_whole_request_premium() -> None:
    pricing = get_model_pricing("claude-sonnet-4-5")
    assert pricing is not None and pricing.known
    assert pricing.long_context_threshold() == 200_000
    assert pricing.request_cost_usd(input_tokens=1_000, long_context=True) > pricing.request_cost_usd(
        input_tokens=1_000
    )


def test_intro_pricing_window_gate() -> None:
    assert _intro_pricing_active("9999-12-31") is True
    assert _intro_pricing_active("2000-01-01") is False
    assert _intro_pricing_active("") is False
    assert _intro_pricing_active("not-a-date") is False


def test_sonnet_5_rates_track_the_intro_window() -> None:
    pricing = get_model_pricing("claude-sonnet-5")
    assert pricing is not None and pricing.known
    if _intro_pricing_active("2026-08-31"):
        expected = (2.0, 10.0, 0.2, 2.5, 4.0, 10.0)
    else:
        expected = (3.0, 15.0, 0.3, 3.75, 6.0, 15.0)
    assert (
        pricing.input,
        pricing.output,
        pricing.cache_read,
        pricing.cache_write,
        pricing.cache_write_1h,
        pricing.thinking,
    ) == expected


def test_display_names_resolve_to_priced_models() -> None:
    for display, canonical in (
        ("Fable 5", "claude-fable-5"),
        ("Mythos 5", "claude-mythos-5"),
        ("Mythos Preview", "claude-mythos-preview"),
        ("Sonnet 5", "claude-sonnet-5"),
        ("Fable 5 (1M context)", "claude-fable-5"),
    ):
        assert resolve_model_id(display) == canonical
        assert get_model_pricing(canonical).known
