---
phase: 12-cache-aware-routing
plan: 01
subsystem: model-routing
tags: [cache-aware-routing, prefix-cache, telemetry, benchmark]

requires:
  - phase: 8-context-lineage
    provides: context-quality benchmark foundation
provides:
  - Cache eviction cost helper for prefix-cache route decisions
  - Sticky routing state primitive for caller-owned follow-up windows
  - Cache-aware ModelRouter.recommend() with route-decision telemetry
  - Deterministic M2 routing benchmark proving cost reduction without quality-tier regression
affects: [phase-13-linear-cache-reuse, phase-14-counterexample-loop, phase-15-proof-gate]

tech-stack:
  added: []
  patterns: [pure pricing helpers, fail-open telemetry sink, deterministic local replay benchmark]

key-files:
  created:
    - src/atelier/core/capabilities/model_routing/cache_cost.py
    - src/atelier/core/capabilities/model_routing/stickiness.py
    - tests/core/test_model_routing_cache_aware.py
  modified:
    - src/atelier/core/capabilities/model_routing/__init__.py
    - src/atelier/core/capabilities/model_routing/router.py
    - src/atelier/core/foundation/models.py
    - src/atelier/gateway/adapters/mcp_server.py
    - tests/benchmarks/context_quality/M2_routing.py
    - tests/gateway/test_mcp_tool_handlers.py

key-decisions:
  - "ModelRouter.score() remained unchanged; cache-aware behavior is exposed through optional ModelRouter.recommend() inputs."
  - "Route-decision telemetry is fail-open through an optional sink and records ledger events only when fallback routing is used by the gateway."
  - "Unknown cache pricing uses a finite conservative fallback so routing never emits inf/nan economics."

patterns-established:
  - "Cache-aware route decisions carry chosen tier, baseline tier, cache cost, estimated quality gain, decision, and sticky remaining count."
  - "Sticky route state is caller-owned and reset outside the router at user-visible response boundaries."

requirements-completed: [CACHE-01, CACHE-02, CACHE-03, CACHE-04, CACHE-05, CQEVAL-03]

duration: 29min
completed: 2026-05-28
---

# Phase 12: Cache-Aware Routing Summary

**Prefix-cache economics and sticky follow-up routing now influence model recommendations without breaking existing score() callers**

## Performance

- **Duration:** 29 min
- **Started:** 2026-05-28T21:18:00Z
- **Completed:** 2026-05-28T21:47:11Z
- **Tasks:** 7
- **Files modified:** 9

## Accomplishments

- Added `cache_eviction_cost_usd()` with cache-write, input-rate, and finite unknown-pricing fallback behavior.
- Added `DEFAULT_STICKINESS_WINDOW = 3` and a typed sticky routing primitive for caller-owned follow-up state.
- Extended `ModelRouter` with cache-aware `recommend()` while preserving `score()` and existing tests.
- Wired fallback gateway routing to use `recommend()` and record `route_decision` ledger events without blocking tool dispatch.
- Replaced the M2 placeholder with a deterministic 50-trace replay benchmark asserting `cost_reduction >= 0.10` and `quality_tier_regressions == 0`.

## Task Commits

1. **Tasks 12-01-01 through 12-01-07: Cache-aware routing implementation and validation** - `9579dad` (feat)

**Plan metadata:** `fea1626` (docs: create cache-aware routing plan)

## Files Created/Modified

- `src/atelier/core/capabilities/model_routing/cache_cost.py` - Pure prefix-cache eviction cost estimator.
- `src/atelier/core/capabilities/model_routing/stickiness.py` - Caller-owned sticky routing state helpers.
- `src/atelier/core/capabilities/model_routing/router.py` - Cache-aware `recommend()` path, route-decision payloads, and cache-aware recommendation fields.
- `src/atelier/core/capabilities/model_routing/__init__.py` - Public exports for cache/stickiness helpers.
- `src/atelier/core/foundation/models.py` - Allows `route_decision` events in the run ledger model.
- `src/atelier/gateway/adapters/mcp_server.py` - Fallback route telemetry now records route decisions through the new recommendation path.
- `tests/core/test_model_routing_cache_aware.py` - Focused cache-cost, stickiness, recommendation, and sink tests.
- `tests/benchmarks/context_quality/M2_routing.py` - Deterministic slow benchmark for M2 routing economics.
- `tests/gateway/test_mcp_tool_handlers.py` - Regression coverage for fallback route-decision logging.

## Decisions Made

Followed the Phase 12 plan decisions D-01 through D-08. No new architectural decisions were introduced.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- Full `make typecheck` remains blocked by pre-existing unrelated issues in `src/atelier/core/capabilities/sync/encryption.py` and a dirty `src/atelier/core/runtime/engine.py` change; Phase 12 focused mypy passes.
- Full `make test` reports unrelated context/docs/memory failures; Phase 12 unit, gateway, and M2 benchmark checks pass.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Phase 13 can now consume cache-aware route telemetry and pricing helpers for the phase-linear cache-reuse agent. The existing dirty Phase 13 files still need careful inspection before editing.

## Self-Check: PASSED

- `uv run pytest tests/core/test_model_router.py tests/core/test_model_routing_cache_aware.py tests/gateway/test_mcp_tool_handlers.py::test_model_recommendation_emitted_before_tool_dispatch tests/gateway/test_mcp_tool_handlers.py::test_model_recommendation_fallback_records_route_decision -q` passed.
- `uv run pytest tests/benchmarks/context_quality/M2_routing.py -m slow -q` passed.
- `uv run ruff check src/atelier/core/foundation/models.py src/atelier/core/capabilities/model_routing src/atelier/gateway/adapters/mcp_server.py tests/core/test_model_routing_cache_aware.py tests/benchmarks/context_quality/M2_routing.py tests/gateway/test_mcp_tool_handlers.py` passed.
- `uv run mypy src/atelier/core/capabilities/model_routing` passed.
- `make lint` passed.

---
*Phase: 12-cache-aware-routing*
*Completed: 2026-05-28*
