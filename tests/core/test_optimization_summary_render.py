"""The advisor block must not announce a recommendation it did not make.

On a real store the block printed:

    Recommended: Custom (auto-tuned from your sessions)
      Cost / week:      $1457.10  (-0%)
      ...
        Total saved:      $0.00/wk

-- a "Recommended" headline over a policy identical to the current one, an
all-zero savings breakdown, and a ``-0%`` that the two costs above it refute.
Plan 2026-09-07 §19 names theoretical savings percentages a vanity metric; a
0% one attached to an empty result is that metric with nothing behind it.

The header lied about its own window too: it said "your last 7 days" whatever
``--days`` the caller passed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import click
from click.testing import CliRunner

from lemoncrow.core.capabilities.reporting.dashboard import _render_optimization_summary


def _candidate(cid: str, cost: float, *, quality: float = 0.983) -> Any:
    return SimpleNamespace(
        id=cid,
        weekly_cost_usd=cost,
        estimated_quality=quality,
        latency_mult=0.75,
        escalation_rate=0.14,
        compaction_breakdown={"prompt_cache_reorder": 1.0, "dedup": 2.0},
        routing_saved_usd=3.0,
        policy=SimpleNamespace(name="Custom"),
    )


def _result(*, current_cost: float, recommended_cost: float, days_sessions: int = 451) -> Any:
    return SimpleNamespace(
        candidates=[_candidate("current", current_cost), _candidate("tuned", recommended_cost)],
        current_policy=SimpleNamespace(name="Balanced"),
        has_recommendation=True,
        baseline_weekly_cost_usd=current_cost,
        weekly_savings_usd=current_cost - recommended_cost,
        quality_delta=0.0,
        message="Need more session history.",
        sessions_analysed=days_sessions,
        replayable_tasks=days_sessions,
        confidence="medium",
        confidence_reason="directional",
        golden=SimpleNamespace(passed=52, total=52, score=1.0),
    )


def _render(result: Any, *, days: int) -> str:
    runner = CliRunner()

    @click.command()
    def _cmd() -> None:
        _render_optimization_summary(result, days=days)

    out = runner.invoke(_cmd)
    assert out.exit_code == 0, out.output
    return out.output


def test_zero_delta_is_reported_as_no_change_not_as_a_recommendation() -> None:
    out = _render(_result(current_cost=1457.10, recommended_cost=1457.10), days=7)

    assert "No change recommended" in out
    assert "Recommended: Custom" not in out
    assert "(-0%)" not in out
    assert "(same as current)" in out
    # The empty savings breakdown goes with the empty recommendation.
    assert "Total saved:" not in out


def test_a_real_delta_prints_dollars_the_reader_can_check() -> None:
    out = _render(_result(current_cost=100.0, recommended_cost=75.0), days=7)

    assert "Recommended: Custom" in out
    assert "$75.00" in out
    # The delta is the difference between the two printed costs, so it can be
    # verified from the screen: 100 - 75 = 25 = 25.0% of 100.
    assert "-25.00/wk vs current" in out
    assert "+25.0%" in out


def test_sub_cent_delta_is_not_dressed_up_as_a_saving() -> None:
    """$0.004/wk rounds to $0.00 in both printed costs; do not claim a win."""

    out = _render(_result(current_cost=1457.104, recommended_cost=1457.100), days=7)
    assert "No change recommended" in out


def test_header_names_the_window_that_was_actually_analysed() -> None:
    out = _render(_result(current_cost=100.0, recommended_cost=75.0), days=30)
    assert "Analysed your last 30 days" in out
    assert "last 7 days" not in out
