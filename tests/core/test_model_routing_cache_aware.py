"""Cache-aware model routing tests."""

from __future__ import annotations

from lemoncrow.core.capabilities.model_routing import (
    DEFAULT_STICKINESS_WINDOW,
    ModelRecommendation,
    ModelRouter,
    cache_eviction_cost_usd,
    decrement_stickiness,
    reset_stickiness,
    start_stickiness,
)
from lemoncrow.core.capabilities.prefix_cache.planner import PrefixCachePlan
from lemoncrow.core.capabilities.pricing import ModelPricing


def _plan(prefix_hash: str, prefix_tokens: int = 100_000) -> PrefixCachePlan:
    return PrefixCachePlan(
        static_prefix=(),
        dynamic_state=(),
        prefix_hash=prefix_hash,
        prefix_tokens=prefix_tokens,
        dynamic_tokens=1_000,
        total_tokens=prefix_tokens + 1_000,
    )


def _pricing(*, cache_write: float = 0.0, input: float = 0.0) -> ModelPricing:
    return ModelPricing(model_id="test-model", input=input, output=0.0, cache_write=cache_write)


def _prior_route() -> ModelRecommendation:
    rec = ModelRouter().score("read", "explain briefly", {"prior_errors": 0})
    assert rec is not None
    return rec


def test_cache_eviction_cost_zero_when_prefix_hash_same() -> None:
    assert cache_eviction_cost_usd(_plan("same"), _plan("same"), _pricing(cache_write=9.0)) == 0.0


def test_cache_eviction_cost_uses_cache_write_rate_when_prefix_changes() -> None:
    assert cache_eviction_cost_usd(_plan("a", 200_000), _plan("b", 100_000), _pricing(cache_write=6.0)) == 1.2


def test_cache_eviction_cost_falls_back_to_input_rate_when_cache_write_unknown() -> None:
    assert cache_eviction_cost_usd(_plan("a", 200_000), _plan("b", 100_000), _pricing(input=3.0)) == 0.6


def test_cache_eviction_cost_biases_sticky_when_pricing_unknown() -> None:
    assert cache_eviction_cost_usd(_plan("a", 200_000), _plan("b", 100_000), _pricing()) == 0.6


def test_recommend_backward_compatible_without_cache_inputs() -> None:
    rec = ModelRouter().recommend("read", "explain this function briefly", {"prior_errors": 0})

    assert rec is not None
    assert rec.decision == "baseline"
    assert rec.tier == "cheap"


def test_stickiness_holds_prior_route_for_window() -> None:
    state = start_stickiness()
    rec = ModelRouter().recommend(
        "Agent",
        "design an end-to-end migration plan",
        {"prior_errors": 3},
        prior_route=_prior_route(),
        stickiness_remaining=state.remaining_tool_calls,
    )

    assert DEFAULT_STICKINESS_WINDOW == 3
    assert rec is not None
    assert rec.decision == "sticky"
    assert rec.tier == "cheap"
    assert rec.sticky_until_tool_calls == 2
    assert decrement_stickiness(state).remaining_tool_calls == 2
    assert reset_stickiness().remaining_tool_calls == 0


def test_stickiness_zero_allows_baseline_scoring() -> None:
    rec = ModelRouter().recommend(
        "Agent",
        "design an end-to-end migration plan",
        {"prior_errors": 3},
        prior_route=_prior_route(),
        stickiness_remaining=0,
    )

    assert rec is not None
    assert rec.decision == "baseline"
    assert rec.tier == "expensive"


def test_cache_cost_beats_quality_gain_preserves_prior_route() -> None:
    rec = ModelRouter().recommend(
        "Agent",
        "design an end-to-end migration plan",
        {"prior_errors": 3, "quality_gain_usd_estimated": 0.1},
        prior_plan=_plan("a", 200_000),
        current_plan=_plan("b", 100_000),
        prior_route=_prior_route(),
        pricing=_pricing(cache_write=6.0),
    )

    assert rec is not None
    assert rec.decision == "cache_preserve"
    assert rec.tier == "cheap"
    assert rec.cache_cost_usd == 1.2
    assert rec.quality_gain_usd_estimated == 0.1


def test_quality_gain_beats_cache_cost_switches_to_baseline() -> None:
    rec = ModelRouter().recommend(
        "Agent",
        "design an end-to-end migration plan",
        {"prior_errors": 3, "quality_gain_usd_estimated": 2.0},
        prior_plan=_plan("a", 200_000),
        current_plan=_plan("b", 100_000),
        prior_route=_prior_route(),
        pricing=_pricing(cache_write=6.0),
    )

    assert rec is not None
    assert rec.decision == "quality_gain"
    assert rec.tier == "expensive"
    assert rec.cache_cost_usd == 1.2
    assert rec.quality_gain_usd_estimated == 2.0


def test_route_decision_sink_called_for_cache_aware_recommend() -> None:
    events: list[dict[str, object]] = []

    rec = ModelRouter().recommend(
        "Agent",
        "design an end-to-end migration plan",
        {"prior_errors": 3, "quality_gain_usd_estimated": 0.1},
        prior_plan=_plan("a", 200_000),
        current_plan=_plan("b", 100_000),
        prior_route=_prior_route(),
        pricing=_pricing(cache_write=6.0),
        route_decision_sink=events.append,
    )

    assert rec is not None
    assert events[0]["kind"] == "route_decision"
    assert events[0]["decision"] == "cache_preserve"
    assert events[0]["baseline_tier"] == "expensive"


def test_trace_calibrated_quality_gain_uses_outcome_delta() -> None:
    rec = ModelRouter().recommend(
        "Agent",
        "design an end-to-end migration plan",
        {"prior_errors": 3, "route_outcome_score_delta": 0.5, "expected_input_tokens": 200_000},
        prior_plan=_plan("a", 200_000),
        current_plan=_plan("b", 100_000),
        prior_route=_prior_route(),
        pricing=_pricing(cache_write=0.5),
    )

    assert rec is not None
    assert rec.decision == "quality_gain"
    assert rec.quality_gain_usd_estimated == 0.2


def test_route_decision_sink_failure_is_swallowed() -> None:
    def fail(_: dict[str, object]) -> None:
        raise RuntimeError("sink down")

    rec = ModelRouter().recommend("read", "explain briefly", {"prior_errors": 0}, route_decision_sink=fail)

    assert rec is not None
    assert rec.decision == "baseline"


def test_to_dict_includes_cache_aware_fields() -> None:
    rec = ModelRouter().recommend("read", "explain briefly", {"prior_errors": 0})

    assert rec is not None
    data = rec.to_dict()
    assert data["cache_cost_usd"] == 0.0
    assert data["quality_gain_usd_estimated"] == 0.0
    assert data["decision"] == "baseline"
    assert data["baseline_tier"] == "cheap"
    assert data["sticky_until_tool_calls"] == 0
