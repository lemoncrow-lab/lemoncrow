"""M2 — Cache-Aware Routing benchmark.

Target: >=10% cost reduction on 50 replayed session traces with no quality-tier regressions.
Baseline: cost without cache-aware routing.
"""

from __future__ import annotations

import pytest

from atelier.core.capabilities.model_routing import ModelRecommendation, ModelRouter
from atelier.core.capabilities.prefix_cache.planner import PrefixCachePlan
from atelier.core.capabilities.pricing import ModelPricing

PRICING = ModelPricing(model_id="m2-routing", input=3.0, output=15.0, cache_write=6.0)
TIER_RANK = {"cheap": 0, "medium": 1, "expensive": 2}


def _plan(prefix_hash: str, prefix_tokens: int) -> PrefixCachePlan:
    return PrefixCachePlan(
        static_prefix=(),
        dynamic_state=(),
        prefix_hash=prefix_hash,
        prefix_tokens=prefix_tokens,
        dynamic_tokens=1_000,
        total_tokens=prefix_tokens + 1_000,
    )


def _trace(index: int) -> tuple[str, str, dict[str, object], PrefixCachePlan]:
    tool_name = "Agent" if index % 5 == 0 else "read"
    task_text = "design an end-to-end migration plan" if tool_name == "Agent" else "explain this function briefly"
    quality_gain = 2.0 if tool_name == "Agent" else 0.05
    state: dict[str, object] = {
        "prior_errors": 3 if tool_name == "Agent" else 0,
        "quality_gain_usd_estimated": quality_gain,
    }
    return tool_name, task_text, state, _plan(f"turn-{index}", 200_000)


def _estimated_cost(rec: ModelRecommendation, plan: PrefixCachePlan) -> float:
    if rec.decision in {"sticky", "cache_preserve"}:
        return PRICING.cost_usd(cache_read_tokens=plan.prefix_tokens, input_tokens=plan.dynamic_tokens)
    return PRICING.cost_usd(cache_write_tokens=plan.prefix_tokens, input_tokens=plan.dynamic_tokens)


@pytest.mark.slow
def test_m2_routing_replay_cost_reduction_without_quality_regression() -> None:
    router = ModelRouter()
    baseline_cost = 0.0
    cache_aware_cost = 0.0
    quality_tier_regressions = 0
    prior_route: ModelRecommendation | None = None
    prior_plan: PrefixCachePlan | None = None
    sticky_remaining = 0

    for index in range(50):
        tool_name, task_text, state, current_plan = _trace(index)
        baseline = router.score(tool_name, task_text, state)
        assert baseline is not None
        baseline_cost += PRICING.cost_usd(
            cache_write_tokens=current_plan.prefix_tokens, input_tokens=current_plan.dynamic_tokens
        )

        cache_aware = router.recommend(
            tool_name,
            task_text,
            state,
            prior_plan=prior_plan,
            current_plan=current_plan,
            prior_route=prior_route,
            stickiness_remaining=sticky_remaining,
            pricing=PRICING,
        )
        assert cache_aware is not None
        cache_aware_cost += _estimated_cost(cache_aware, current_plan)
        if TIER_RANK[cache_aware.tier] < TIER_RANK[baseline.tier]:
            quality_tier_regressions += 1
        sticky_remaining = cache_aware.sticky_until_tool_calls
        prior_route = cache_aware
        prior_plan = current_plan

    cost_reduction = round((baseline_cost - cache_aware_cost) / baseline_cost, 4)
    result = {
        "baseline_cost": round(baseline_cost, 8),
        "cache_aware_cost": round(cache_aware_cost, 8),
        "cost_reduction": cost_reduction,
        "quality_tier_regressions": quality_tier_regressions,
    }
    print(result)

    assert cost_reduction >= 0.10
    assert quality_tier_regressions == 0
