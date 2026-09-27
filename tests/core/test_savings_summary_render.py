"""Unit tests for render_savings_summary (default `lc savings` view)."""

from __future__ import annotations

from typing import Any

from lemoncrow.core.capabilities.savings_summary import render_savings_markdown, render_savings_summary


def _payload(
    *,
    lifetime_saved: float,
    saved_30d: float,
    spend_30d: float | None,
    unpriced: int = 0,
    rows: int = 0,
) -> dict[str, Any]:
    head: dict[str, Any] = {"calls": 10, "usd": saved_30d, "tokens": 100}
    if spend_30d is None:
        head["spend_available"] = False
    else:
        head.update({"spend": spend_30d, "spend_available": True, "spend_rows": rows, "spend_unpriced_rows": unpriced})
    return {
        "saved_usd": lifetime_saved,
        "calls_avoided": 10,
        "tokens_saved": 1_000,
        "spend_source": "usage_read_model",
        "summary_breakdown": {
            "1D": {"calls": 1, "usd": 1.0, "tokens": 10, "spend": 2.0},
            "7D": {"calls": 5, "usd": 5.0, "tokens": 50, "spend": 10.0},
            "30D": head,
        },
    }


def test_headline_is_one_window_and_arithmetically_closed() -> None:
    """The headline block must be checkable by eye and true.

    The old line read ``Saved $X (P% of $S spend · 30d)`` where ``X`` was the
    LIFETIME total and ``P``/``S`` were 30-day figures. On a real store it
    printed ``$20,421.59 (44.5% of $19,070.16 spend)`` -- a saving larger than
    the spend it claimed to be 44.5% of -- and the actual numerator turned up
    unannounced in the window table below. Every figure in the block now comes
    from the one named window and the three dollars add up.
    """
    out = render_savings_summary(_payload(lifetime_saved=100.0, saved_30d=10.0, spend_30d=40.0))

    assert "Window              last 30d" in out
    assert "Spend                 $40.00" in out
    assert "Saved (est.)          $10.00" in out
    assert "Baseline              $50.00" in out  # 40 + 10
    assert "Share                  20.0%" in out  # 10 / 50
    # The lifetime figure is present but never the subject of the windowed ratio.
    assert "Lifetime saved (all history, modelled): $100.00." in out
    assert "of $40.00 spend" not in out


def _cell(rendered: str, label: str) -> str:
    """The value printed on the headline row named *label*."""

    line = next(one for one in rendered.splitlines() if one.strip().startswith(label))
    return line.split()[-1]


def _idle_payload() -> dict[str, Any]:
    """A store with history but nothing at all in the last 30 days."""

    payload = _payload(lifetime_saved=1_234.0, saved_30d=0.0, spend_30d=0.0)
    payload["calls_avoided"] = 9_876
    payload["tokens_saved"] = 45_000_000
    payload["summary_breakdown"]["30D"].update({"calls": 0, "tokens": 0, "usd": 0.0})
    return payload


def test_headline_counts_are_the_windows_own_never_the_lifetime_ones() -> None:
    """A legitimate 0 in the window bucket is a fact, not a missing value.

    ``int(head.get("calls") or payload.get("calls_avoided") or 0)`` fell
    through to the LIFETIME count whenever the window's own count was 0, so a
    paused project printed ``Window last 30d / Calls avoided 9,876 / Tokens
    kept out 45.00M`` while the by-window table on the same screen showed
    ``30 days ... 0  0`` for the very same window.
    """
    out = render_savings_summary(_idle_payload())

    assert "Window              last 30d" in out
    assert _cell(out, "Calls avoided") == "0"
    assert _cell(out, "Tokens kept out") == "0"
    assert "9,876" not in out
    assert "45.00M" not in out
    # Lifetime keeps its own labelled line -- it is never suppressed, only
    # kept out of the windowed block.
    assert "Lifetime saved (all history, modelled): $1,234.00." in out


def test_markdown_headline_counts_are_the_windows_own_too() -> None:
    """The markdown twin is what the ``statusline_segment`` MCP tool serves."""
    out = render_savings_markdown(_idle_payload())

    assert "| Calls avoided | 0 | |" in out
    assert "| Tokens kept out | 0 | |" in out
    assert "9,876" not in out
    assert "45.00M" not in out


def test_a_non_zero_window_still_reports_its_own_counts() -> None:
    """The fix must not zero a window that really did have activity."""
    out = render_savings_summary(_payload(lifetime_saved=100.0, saved_30d=10.0, spend_30d=40.0))
    assert _cell(out, "Calls avoided") == "10"
    assert _cell(out, "Tokens kept out") == "100"


def test_saved_is_labelled_as_a_counterfactual_not_money() -> None:
    out = render_savings_summary(_payload(lifetime_saved=100.0, saved_30d=10.0, spend_30d=40.0))
    assert "modelled counterfactual, not billed" in out
    assert "cannot be subtracted from Spend" in out
    assert "= Spend + Saved" in out
    assert "= Saved / Baseline" in out


def test_spend_provenance_is_named_only_when_the_payload_earned_it() -> None:
    reconciled = render_savings_summary(_payload(lifetime_saved=100.0, saved_30d=10.0, spend_30d=40.0))
    assert "same rows as `lc usage --since 30d`" in reconciled

    legacy = _payload(lifetime_saved=100.0, saved_30d=10.0, spend_30d=40.0)
    del legacy["spend_source"]
    assert "same rows as `lc usage" not in render_savings_summary(legacy)


def test_unpriced_rows_qualify_the_denominator() -> None:
    out = render_savings_summary(_payload(lifetime_saved=100.0, saved_30d=10.0, spend_30d=40.0, unpriced=7, rows=99))
    assert "7 of 99 rows in this window carry no price" in out
    assert "Spend is a floor and Share an upper bound" in out


def test_unavailable_spend_renders_as_unknown_never_as_zero() -> None:
    """A store the read model cannot answer for has UNKNOWN spend, not $0.00.

    Rendering zero would turn a missing signal into the false claim that the
    window was free -- and would then make Share read 100%.
    """
    out = render_savings_summary(_payload(lifetime_saved=100.0, saved_30d=10.0, spend_30d=None))
    assert "Spend            unavailable   the usage read model could not be read" in out
    assert "Baseline" not in out
    assert "Share" not in out
    assert "$0.00" not in out
    assert "n/a" in out.split("By window")[1]  # ...and the window row says so too


def test_window_table_carries_spend_so_the_headline_is_one_of_its_rows() -> None:
    out = render_savings_summary(_payload(lifetime_saved=100.0, saved_30d=10.0, spend_30d=40.0))
    table = out.split("By window")[1]
    assert "spend" in out.split("By window")[0] or "spend" in table
    assert "$40.00" in table  # the 30-day row repeats the headline spend
    assert "$10.00" in table  # ...and the headline saved


def test_legacy_subscription_blob_is_ignored() -> None:
    payload = _payload(lifetime_saved=14.2, saved_30d=14.2, spend_30d=20.0)
    payload["subscription"] = {
        "plan": "free",
        "monthlySavingsCapInUsd": 20.0,
        "monthlySavingsInUsd": 22.9,
        "savingsOverCap": True,
    }
    out = render_savings_summary(payload)
    assert "Plan" not in out
    assert "CAP REACHED" not in out
    assert "dormant" not in out
