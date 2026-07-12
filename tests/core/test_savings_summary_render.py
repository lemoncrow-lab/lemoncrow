"""Unit tests for render_savings_summary (default `lc savings` view)."""

from __future__ import annotations

from typing import Any

from lemoncrow.core.capabilities.savings_summary import render_savings_summary


def _payload(*, lifetime_saved: float, saved_30d: float, spend_30d: float) -> dict[str, Any]:
    return {
        "saved_usd": lifetime_saved,
        "calls_avoided": 10,
        "tokens_saved": 1_000,
        "summary_breakdown": {
            "1D": {"calls": 1, "usd": 1.0, "tokens": 10, "spend": 2.0},
            "7D": {"calls": 5, "usd": 5.0, "tokens": 50, "spend": 10.0},
            "30D": {"calls": 10, "usd": saved_30d, "tokens": 100, "spend": spend_30d},
        },
    }


def test_headline_pct_uses_30d_saved_not_lifetime() -> None:
    """Regression: history longer than 30d must not inflate the headline pct.

    Lifetime saved ($100) is 10x the 30-day saved ($10); the percentage is
    labeled "· 30d" so it must be 10/40 = 25.0%, not 100/40 = 250.0%.
    """
    out = render_savings_summary(_payload(lifetime_saved=100.0, saved_30d=10.0, spend_30d=40.0))
    assert "(25.0% of $40.00 spend · 30d)" in out
    assert "250.0%" not in out
    # Headline dollar figure stays lifetime.
    assert "Saved            $100.00" in out


def test_headline_no_pct_without_30d_spend() -> None:
    out = render_savings_summary(_payload(lifetime_saved=100.0, saved_30d=10.0, spend_30d=0.0))
    assert "spend · 30d" not in out
    assert "Saved            $100.00" in out
