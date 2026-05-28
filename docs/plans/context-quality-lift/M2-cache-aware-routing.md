# M2 — Cache-aware model routing (router ↔ prefix-cache integration)

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).

## Goal

The router stops switching models when the KV-cache eviction cost exceeds the expected quality gain. Routes are sticky within a single agent turn's tool-call chain. Net effect: cheaper retries, fewer cache-miss tail latencies, no quality loss.

## Augment reference

[Augment Prism blog](https://www.augmentcode.com/blog/augment-prism-model-routing-to-reduce-cost-and-maintain-quality):

> Routing decisions are sticky across tool-call follow-ups within a single agent turn (prevents cache thrashing). Cache-aware: the planner only switches models when expected quality gain exceeds KV-cache eviction cost.

Reported result: SWE-bench Pro 52.9% at −12% cost (GPT+Kimi pool), 59.5% at −7% cost (Claude+Gemini pool). Routing planner overhead: ~0.03% of spend, runs on 4% of turns.

## Background

Both pieces already exist in Atelier; they just don't talk:

- `core/capabilities/prefix_cache/planner.py` produces a `PrefixCachePlan` with `prefix_hash`, `invalidated_reason`, `prefix_tokens`, `dynamic_tokens`. It can already detect whether a candidate plan would invalidate a prior turn's prefix.
- `core/capabilities/model_routing/router.py` returns a `RouteRecommendation` with a tier ∈ {deterministic, local_slm, cheap_llm, frontier_llm, human_review}.

Neither component knows the other exists. The router can pick a different model on every turn; the prefix-cache planner is consulted (if at all) after the fact.

## Module layout

```
src/atelier/core/capabilities/model_routing/
  router.py            (extend)  — accept optional PrefixCachePlan; respect stickiness window
  cache_cost.py        (new)     — pure function: cache_eviction_cost_usd(plan_a, plan_b, pricing)
  stickiness.py        (new)     — turn-window state: same model for follow-up tool calls
src/atelier/core/capabilities/prefix_cache/
  planner.py           (no change) — already produces what we need
```

No new MCP tool. The change surface is `router.recommend(...)` signature extension.

## API change

```python
@dataclass
class RouteRecommendation:
    tier: RouteTier
    model_hint: str | None
    rationale: str
    cache_cost_usd: float = 0.0       # NEW — estimated KV-cache eviction cost if we switch
    sticky_until_tool_calls: int = 0  # NEW — caller should reuse this route for N more tool calls

class ModelRouter:
    def recommend(
        self,
        *,
        prior_plan: PrefixCachePlan | None = None,  # NEW
        current_plan: PrefixCachePlan | None = None, # NEW
        prior_route: RouteRecommendation | None = None,  # NEW — for stickiness
        stickiness_remaining: int = 0,  # NEW — caller-tracked counter
        ...existing args,
    ) -> RouteRecommendation:
        ...
```

## Decision rule (the load-bearing change)

```
If stickiness_remaining > 0 and prior_route exists:
    return prior_route with sticky_until_tool_calls = stickiness_remaining - 1

baseline_route = existing tier logic

if prior_plan and current_plan:
    cache_cost = cache_eviction_cost_usd(prior_plan, current_plan, pricing_table)
    expected_quality_gain_usd = estimate_quality_value(baseline_route, prior_route)
    if cache_cost > expected_quality_gain_usd:
        # Stay on prior_route to preserve cache
        return prior_route.with_rationale(f"cache-sticky: switching would cost ${cache_cost:.4f}")

return baseline_route.with_stickiness(default_stickiness_window=3)
```

`estimate_quality_value` is intentionally simple in M2: a static table per (from_tier, to_tier) pair. M3 may replace it with a learned estimate; not in scope here.

## Pricing source

Reuse `core/capabilities/pricing.yaml`. Add per-million-token cache-read vs. cache-write cost rows where the provider distinguishes them (Anthropic does). Where unknown, assume cache-write cost == full-input cost (worst case — biases toward stickiness, which is the safe direction).

## Stickiness window default

Default: **3 follow-up tool calls.** Rationale: most agent turns are read → search → edit, all of which should run on the same model to keep the cache hot. Configurable via `pricing.yaml → routing.stickiness_window`.

Stickiness resets when the agent emits a new user-visible response (not when it emits a tool call).

## Telemetry

Every `recommend()` call emits a record:

```json
{
  "event": "route_decision",
  "chosen_tier": "cheap_llm",
  "baseline_tier": "frontier_llm",
  "cache_cost_usd": 0.0042,
  "quality_gain_usd_estimated": 0.0018,
  "decision": "sticky_baseline",
  "stickiness_remaining": 2
}
```

Persisted to the existing run-ledger session file. Feeds the savings dashboard.

## Validation

Tests under `tests/core/test_model_routing_cache_aware.py`:

- `test_stickiness_holds_route_for_window` — three consecutive tool calls return the same model.
- `test_cache_cost_beats_quality_gain` — synthetic plans where switching evicts a 50k-token prefix; assert router stays put.
- `test_quality_gain_beats_cache_cost` — synthetic plans where switching saves on a high-value frontier query; assert router switches.
- `test_telemetry_emitted_per_call` — `route_decision` event present in run ledger.

Benchmark under `tests/benchmarks/context_quality/M2_routing.py`:

- Replay 50 prior session traces through the new router.
- Metric: total estimated cost (with switching) vs. cost with cache-aware stickiness.
- Target: ≥10% cost reduction with no quality-tier regressions.

## Exit criteria

- API extended; existing callers compile without change (new args default to None).
- Stickiness window default 3; configurable.
- Telemetry rows present in ledger.
- Benchmark target hit (≥10% replay cost reduction).
- No regression in `tests/core/test_model_routing.py`.

## Open questions

- Should `estimate_quality_value` be deterministic (static table) or learned (regression on past traces)? M2 ships deterministic; learned is a follow-up.
- How do we surface stickiness to host CLIs (Claude Code, Codex) that own model selection? Probably via an explicit `model_hint` field they may ignore. Document this honestly — the router is advisory.
