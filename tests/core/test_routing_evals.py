from __future__ import annotations

from atelier.core.capabilities.quality_router.evals import (
    RoutingEvalCase,
    summarize_routing_evals,
)


def test_routing_evals_successful_cheap_route() -> None:
    summary = summarize_routing_evals(
        "run-cheap",
        [
            RoutingEvalCase(tier="cheap", accepted=True, cost_usd=0.12),
            RoutingEvalCase(tier="mid", accepted=True, cost_usd=0.30),
        ],
    )

    assert summary.cheap_success_rate == 1.0
    assert summary.premium_call_rate == 0.0
    assert summary.escalation_success_rate == 0.0
    assert summary.cost_per_accepted_patch == 0.21


def test_routing_evals_failed_cheap_with_premium_recovery() -> None:
    summary = summarize_routing_evals(
        "run-recovery",
        [
            RoutingEvalCase(tier="cheap", accepted=False, cost_usd=0.15),
            RoutingEvalCase(tier="premium", accepted=True, cost_usd=1.05, escalated=True),
        ],
    )

    # Failed cheap attempt must reduce cheap success and still count in total cost.
    assert summary.cheap_success_rate == 0.0
    assert summary.premium_call_rate == 0.5
    assert summary.escalation_success_rate == 1.0
    assert summary.cost_per_accepted_patch == 1.2


def test_routing_evals_regression_rate() -> None:
    summary = summarize_routing_evals(
        "run-regression",
        [
            RoutingEvalCase(tier="cheap", accepted=False, cost_usd=0.2, regression=True),
            RoutingEvalCase(tier="mid", accepted=True, cost_usd=0.5),
            RoutingEvalCase(tier="premium", accepted=True, cost_usd=1.0, escalated=True),
        ],
    )

    assert summary.routing_regression_rate == 0.333333
    assert summary.cheap_success_rate == 0.0
