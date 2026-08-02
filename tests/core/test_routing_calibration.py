from __future__ import annotations

from types import SimpleNamespace

from lemoncrow.pro.capabilities.optimization.routing_calibration import (
    choose_calibrated_route,
    record_route_outcome,
    summarize_route_outcomes,
)


def _decision():
    return SimpleNamespace(
        provider="anthropic",
        model="sonnet",
        tier="high",
        projected_session_cost_usd=0.08,
        alternatives=(
            SimpleNamespace(
                provider="anthropic",
                model="haiku",
                tier="cheap",
                estimated_cost_usd=0.03,
            ),
            SimpleNamespace(
                provider="anthropic",
                model="sonnet",
                tier="high",
                estimated_cost_usd=0.08,
            ),
        ),
    )


def test_calibrated_route_stays_shadow_only_below_sample_floor(tmp_path) -> None:
    route = choose_calibrated_route(
        tmp_path,
        route_decision=_decision(),
        phase="execute",
        current_model="anthropic/sonnet",
        context_tokens=0,
    )
    assert route.model == "haiku"
    assert not route.eligible
    assert route.sample_count == 0


def test_calibrated_route_uses_observed_success_and_failure_cost(tmp_path) -> None:
    for _ in range(20):
        record_route_outcome(
            tmp_path,
            provider="anthropic",
            model="anthropic/haiku",
            phase="execute",
            success=True,
            cost_usd=0.03,
        )
    successful = choose_calibrated_route(
        tmp_path,
        route_decision=_decision(),
        phase="execute",
        current_model="anthropic/sonnet",
        context_tokens=0,
    )
    assert successful.model == "haiku"
    assert successful.eligible
    assert successful.failure_probability < 0.1

    failed_root = tmp_path / "failed"
    for _ in range(20):
        record_route_outcome(
            failed_root,
            provider="anthropic",
            model="anthropic/haiku",
            phase="execute",
            success=False,
            cost_usd=0.03,
        )
    failed = choose_calibrated_route(
        failed_root,
        route_decision=_decision(),
        phase="execute",
        current_model="anthropic/sonnet",
        context_tokens=0,
    )
    assert failed.model == "sonnet"
    assert not failed.eligible


def test_repair_route_escalates_without_waiting_for_samples(tmp_path) -> None:
    route = choose_calibrated_route(
        tmp_path,
        route_decision=_decision(),
        phase="repair",
        current_model="anthropic/haiku",
        context_tokens=10_000,
        failure_count=1,
    )
    assert route.model == "sonnet"
    assert route.tier == "high"
    assert route.eligible
    assert "repair safety gate" in route.reason


def test_route_hysteresis_retains_a_warm_lane_for_marginal_savings(tmp_path) -> None:
    decision = _decision()
    for model, cost in (("anthropic/haiku", 0.074), ("anthropic/sonnet", 0.08)):
        for _ in range(20):
            record_route_outcome(
                tmp_path,
                provider="anthropic",
                model=model,
                phase="execute",
                success=True,
                cost_usd=cost,
            )

    route = choose_calibrated_route(
        tmp_path,
        route_decision=decision,
        phase="execute",
        current_model="anthropic/sonnet",
        context_tokens=0,
    )

    assert route.model == "sonnet"
    assert route.eligible
    assert "hysteresis" in route.reason


def test_route_outcome_summary_is_aggregate_only(tmp_path) -> None:
    record_route_outcome(
        tmp_path,
        provider="openai",
        model="openai/gpt-5",
        phase="finish",
        success=True,
        cost_usd=0.2,
    )
    summary = summarize_route_outcomes(tmp_path)
    assert summary["outcomes"] == 1
    assert summary["success_rate"] == 1.0
    assert summary["cost_usd"] == 0.2
