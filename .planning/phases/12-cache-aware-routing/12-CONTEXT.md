# Phase 12: Cache-Aware Routing - Context

**Gathered:** 2026-05-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 12 delivers cache-aware model routing only: extend the existing `ModelRouter` so it can account for prefix-cache eviction cost, preserve sticky routes across tool-call follow-ups, and emit route-decision telemetry. It does not implement the phase-linear runner, counterexample retry loop, or scoped context pull API; those are Phase 13-15.

</domain>

<decisions>
## Implementation Decisions

### Routing Economics
- **D-01:** Use the locked M2 design: compare `cache_eviction_cost_usd(prior_plan, current_plan, pricing)` against a deterministic `estimated_quality_gain_usd` table. If eviction cost is higher, preserve the prior route.
- **D-02:** The cache-cost calculation must be a pure function in `src/atelier/core/capabilities/model_routing/cache_cost.py`. It should consume `PrefixCachePlan` data and `ModelPricing`, and it must bias toward safe stickiness when cache pricing is unknown.
- **D-03:** Keep `ModelRouter.score()` backward-compatible. New inputs must be optional and default to current behavior so existing callers and tests compile unchanged.

### Stickiness
- **D-04:** Add a default sticky window of three follow-up tool calls. A sticky prior route wins before baseline scoring unless the prior route is missing or the remaining counter is zero.
- **D-05:** Stickiness reset is caller/runtime-owned at user-visible response boundaries. Phase 12 provides the state primitive and router behavior; broader agent-loop orchestration can wire reset points later.

### Telemetry
- **D-06:** Every cache-aware recommendation should produce a structured `route_decision` payload with chosen/baseline tier, cache cost, estimated quality gain, decision, and remaining stickiness.
- **D-07:** Keep telemetry fail-open. Routing must not fail because a ledger/event sink is unavailable.

### Benchmark
- **D-08:** Replace the M2 placeholder with a deterministic local replay benchmark that proves at least 10% estimated cost reduction and no quality-tier regressions. It should not require remote model calls.

### the agent's Discretion
The user explicitly delegated all implementation choices already decided in docs. The planner/executor may choose the exact static quality-gain values, ledger adapter shape, and test fixture structures, provided they satisfy the canonical docs and benchmark target.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Product and roadmap scope
- `.planning/PROJECT.md` — v0.3 milestone goal, constraints, proof target, and active decisions.
- `.planning/REQUIREMENTS.md` — CACHE-01 through CACHE-05 and CQEVAL-03 requirements.
- `.planning/ROADMAP.md` — Phase 12 boundary, success criteria, and dependency notes.
- `docs/plans/context-quality-lift/M2-cache-aware-routing.md` — locked design for router/prefix-cache integration.

### Existing code to reuse
- `src/atelier/core/capabilities/model_routing/router.py` — current `ModelRouter`, `ModelRecommendation`, route-tier mapping, cache-affinity handling, and scoring behavior.
- `src/atelier/core/capabilities/prefix_cache/planner.py` — `PrefixCachePlan` shape with `prefix_hash`, `prefix_tokens`, `dynamic_tokens`, `total_tokens`, and `invalidated_reason`.
- `src/atelier/core/capabilities/pricing.py` — `ModelPricing`, cache-read/cache-write rates, and token-to-USD conversion.
- `src/atelier/core/runtime/engine.py` — runtime orchestrator where future route-state integration will live.
- `tests/core/test_model_router.py` — existing regression tests for router behavior.
- `tests/benchmarks/context_quality/M2_routing.py` — placeholder benchmark to replace/extend.

### Codebase maps
- `.planning/codebase/STACK.md` — Python 3.11+, uv, pytest, strict mypy, LiteLLM pricing dependency.
- `.planning/codebase/ARCHITECTURE.md` — capability-per-module architecture and thin gateway constraint.
- `.planning/codebase/INTEGRATIONS.md` — provider/pricing and telemetry integration surfaces.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `ModelRouter.score(tool_name, task_text, session_state)` already centralizes advisory routing and has a cache-affinity hook. Extend it rather than adding a parallel router.
- `ModelRecommendation.to_dict()` is the natural output/telemetry serialization point; add cache-aware fields there if they are part of the recommendation contract.
- `PrefixCachePlan` already exposes the stable-prefix token counts and invalidation reason needed for eviction-cost estimation.
- `ModelPricing.cost_usd()` and `tokens_to_usd()` already understand `cache_read` and `cache_write` token classes.

### Established Patterns
- Core capability logic belongs under `src/atelier/core/capabilities/`; gateway files stay thin.
- Existing router behavior returns `None` in bench-off mode. Preserve that behavior.
- Tests should focus on pure functions and deterministic fixtures; no remote model calls are needed.

### Integration Points
- `src/atelier/core/capabilities/model_routing/__init__.py` should export any new public helpers needed by tests or future runtime wiring.
- Runtime ledger/event integration should be optional/fail-open so routing remains a pure advisory path when no ledger is supplied.
- The M2 benchmark lives in `tests/benchmarks/context_quality/M2_routing.py` and should be runnable locally with `pytest -m slow`.

</code_context>

<specifics>
## Specific Ideas

User direction: "Finish context-quality capabilities: cache-aware routing, counterexample loop, scoped pull context" and use `docs/plans/phase-linear-cache-reuse` as locked source material. For this phase, prioritize the M2 routing design exactly over alternative router rewrites.

</specifics>

<deferred>
## Deferred Ideas

- Phase-linear Survey->Plan cache reuse belongs to Phase 13.
- Counterexample retry loop belongs to Phase 14.
- Scoped context pull and final TerminalBench-oriented proof gate belong to Phase 15.

</deferred>

---

*Phase: 12-Cache-Aware Routing*
*Context gathered: 2026-05-28*
