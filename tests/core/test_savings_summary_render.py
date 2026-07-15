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


def _with_sub(sub: dict[str, Any]) -> str:
    payload = _payload(lifetime_saved=14.2, saved_30d=14.2, spend_30d=20.0)
    payload["subscription"] = sub
    return render_savings_summary(payload)


def test_cap_line_free_under_cap() -> None:
    out = _with_sub(
        {
            "plan": "free",
            "monthlySavingsCapInUsd": 20.0,
            "monthlySavingsInUsd": 14.2,
            "savingsRemainingUsd": 5.8,
            "savingsCapFraction": 0.71,
            "savingsOverCap": False,
            "windowDays": 30,
        }
    )
    assert "Cap   $14.20 of $20.00 (30d)" in out
    assert "71.0% used, $5.80 left" in out
    assert "[local est.]" in out
    assert "CAP REACHED" not in out


def test_cap_line_over_cap_shows_dormant_and_server_source() -> None:
    out = _with_sub(
        {
            "plan": "free",
            "monthlySavingsCapInUsd": 20.0,
            "monthlySavingsInUsd": 22.9,
            "savingsOverCap": True,
            "windowDays": 30,
            "savingsMeterSource": "server",
        }
    )
    assert "CAP REACHED · LemonCrow dormant" in out
    assert "[server]" in out


def test_cap_line_lite_uses_its_own_cap() -> None:
    out = _with_sub(
        {
            "plan": "lite",
            "monthlySavingsCapInUsd": 200.0,
            "monthlySavingsInUsd": 40.0,
            "savingsRemainingUsd": 160.0,
            "savingsOverCap": False,
            "windowDays": 30,
        }
    )
    assert "of $200.00" in out  # lite's $200 cap, not free's $20
    assert "$160.00 left" in out


def test_cap_line_pro_is_uncapped() -> None:
    out = _with_sub({"plan": "pro", "monthlySavingsCapInUsd": None, "savingsMeterSource": "server"})
    assert "Cap   uncapped" in out
    assert "CAP REACHED" not in out
