---
phase: "02"
plan: "02"
subsystem: code-context
tags: [m12, cache, diagnostics, budget, benchmarks]
requires:
  - "02-01"
provides:
  - additive cache diagnostics on the existing `code` surface
  - frozen budget/default-policy behavior for shipped search and pattern flows
  - M12 partial-close benchmark and validation evidence
affects: [02-03, 02-04, code-context]
tech-stack:
  added:
    - cross-op cost-discipline benchmark
  patterns:
    - Phase 1 cache/provenance/token metadata reused for diagnostics
    - benchmark/validation evidence recorded as an explicit partial M12 close
key-files:
  created:
    - src/benchmarks/code_intel/cost_discipline.py
    - tests/benchmarks/code_intel/test_cost_discipline.py
  modified:
    - src/atelier/core/capabilities/code_context/cache.py
    - src/atelier/core/capabilities/code_context/budget.py
    - src/atelier/core/capabilities/code_context/engine.py
    - src/atelier/gateway/adapters/mcp_server.py
    - tests/core/test_code_context.py
    - tests/gateway/test_p0_mcp_surfaces.py
    - tests/gateway/test_mcp_tool_handlers.py
    - docs/agent-os/validation-matrix.md
    - .planning/phases/02-structural-discovery-symbol-safe-change-flows/02-VALIDATION.md
key-decisions:
  - "M12 is only a partial close in 02-02; Plans 02-03 and 02-04 still own symbol-edit and usages follow-through."
  - "Cache diagnostics stay additive on `code` and never expose cached payload bodies."
patterns-established:
  - "Phase 2 cost-discipline evidence uses the existing code-intel benchmark stack instead of a parallel benchmark path."
  - "Cache status/invalidation returns scoped metadata plus frozen drop-stage context, not raw cached payloads."
requirements-completed: []
duration: 22min
completed: 2026-05-19
---

# Phase 2 Plan 2: Structural Discovery & Symbol-Safe Change Flows Summary

**M12 core freeze is now partially closed with additive cache diagnostics, frozen low-token defaults for shipped flows, and deterministic cost-discipline benchmark evidence**

## Performance

- **Duration:** 22 min
- **Started:** 2026-05-19T01:32:46+02:00
- **Completed:** 2026-05-19T01:54:54+02:00
- **Tasks:** 2
- **Files modified:** 11

## Accomplishments

- Froze cache key shape, budget packing order, and low-token default behavior for the currently shipped search and pattern flows.
- Added additive `code`-surface diagnostics for cache status and cache invalidation without exposing cached payload bodies.
- Added a deterministic cost-discipline benchmark and validation guidance that records M12 as a partial close pending the symbol-edit and usages plans.

## Task Commits

1. **Task 1: Freeze cache keys, packing order, and additive diagnostics on existing code-intel surfaces**
   - `248f243` (`test`) add failing cache freeze diagnostics coverage
   - `c207a58` (`feat`) freeze cache diagnostics and budget defaults
2. **Task 2: Add cost-discipline benchmarks and record the M12 partial-close contract**
   - `62e96f8` (`feat`) add M12 partial-close benchmark evidence
   - `5ab8bf4` (`fix`) satisfy cache freeze validation gates

## Files Created/Modified

- `src/atelier/core/capabilities/code_context/cache.py` - frozen cache key shape plus scoped stats/invalidation helpers
- `src/atelier/core/capabilities/code_context/budget.py` - frozen drop-order constants for low-token payload policy
- `src/atelier/core/capabilities/code_context/engine.py` - additive `tool_cache_status` / `tool_cache_invalidate` wrappers and shared policy/finalization plumbing
- `src/atelier/gateway/adapters/mcp_server.py` - additive diagnostic dispatch on the existing `code` MCP surface
- `src/benchmarks/code_intel/cost_discipline.py` - aggregate benchmark comparing shipped Phase 2 flows with pre-code-intel baselines
- `tests/core/test_code_context.py` - cache diagnostics and default-vs-heavy payload regressions
- `tests/gateway/test_p0_mcp_surfaces.py` and `tests/gateway/test_mcp_tool_handlers.py` - MCP boundary coverage for additive diagnostics
- `tests/benchmarks/code_intel/test_cost_discipline.py` - aggregate `<=30% of baseline` benchmark gate
- `docs/agent-os/validation-matrix.md` - explicit M12 partial-close validation command and trace expectation
- `.planning/phases/02-structural-discovery-symbol-safe-change-flows/02-VALIDATION.md` - partial-close contract that keeps 02-03 and 02-04 responsible for final M12 follow-through

## Decisions Made

- Kept the diagnostic surface on the existing `code` tool, matching the grounded “extend existing MCP surfaces” rule.
- Recorded M12 as a partial close only, so later plans must still prove edit/usages defaults, diagnostics, and trace evidence before the milestone can be called complete.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Finished validation cleanup after executor cancellation**
- **Found during:** manual closeout after the background executor was cancelled
- **Issue:** the main M12 implementation commits landed, but final validation left small import/export normalization changes in `budget.py` and `engine.py` uncommitted.
- **Fix:** committed the cleanup as `5ab8bf4` so the targeted suite, lint, and typecheck all pass on a clean worktree.
- **Files modified:** `src/atelier/core/capabilities/code_context/budget.py`, `src/atelier/core/capabilities/code_context/engine.py`
- **Verification:** `uv run pytest tests/core/test_code_context.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_tool_handlers.py tests/benchmarks/code_intel/test_cost_discipline.py -q`, `make lint`, and `make typecheck`

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** No scope change. Manual recovery only completed final validation cleanup and documentation after the executor cancellation.

## Issues Encountered

- The background executor was cancelled before it could write `02-02-SUMMARY.md` or finish closeout, so the plan was recovered manually via commit/summary spot-check and inline validation.

## User Setup Required

- None for automated closeout. The remaining manual M12 follow-through items stay with Plans `02-03` and `02-04`.

## Next Phase Readiness

- The code-intel baseline is now frozen for the shipped search and pattern flows, which reduces churn before symbol-edit work lands in `02-03`.
- Validation and benchmark docs now explicitly preserve the partial-close contract so later plans cannot accidentally overclaim full M12 completion.

## Known Stubs

- Full M12 closure is intentionally deferred until `02-03` and `02-04`.

## Self-Check: PASSED

- FOUND: `.planning/phases/02-structural-discovery-symbol-safe-change-flows/02-02-SUMMARY.md`
- FOUND commits: `248f243`, `c207a58`, `62e96f8`, `5ab8bf4`
