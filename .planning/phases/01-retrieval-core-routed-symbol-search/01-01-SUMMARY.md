---
phase: 01-retrieval-core-routed-symbol-search
plan: "01"
subsystem: infra
tags: [code-intel, code-context, caching, token-budget, benchmarks]
requires: []
provides:
  - Retrieval cache and wrapper-aware budget fitting for existing `code` ops
  - Gateway regressions for cache/provenance metadata and savings telemetry
  - Deterministic Phase 1 symbol-search benchmark smoke harness
affects: [01-02, 01-03, code-context]
tech-stack:
  added: []
  patterns:
    - Wrapper-inclusive budget fitting for cached code-intel payloads
    - Deterministic repeated-search smoke benchmarks under `src/benchmarks/code_intel/`
key-files:
  created:
    - src/atelier/core/capabilities/code_context/budget.py
    - src/atelier/core/capabilities/code_context/cache.py
    - src/benchmarks/code_intel/symbol_search_bench.py
    - tests/benchmarks/code_intel/test_symbol_search_bench.py
  modified:
    - src/atelier/core/capabilities/code_context/__init__.py
    - src/atelier/core/capabilities/code_context/engine.py
    - src/atelier/core/capabilities/code_context/models.py
    - tests/core/test_code_context.py
    - tests/gateway/test_p0_mcp_surfaces.py
    - tests/gateway/test_savings_api.py
key-decisions:
  - "Budget-fit helpers now search for the largest packed payload whose final wrapper still fits the declared token budget."
  - "Phase 1 benchmark coverage starts with deterministic local symbol-search smoke checks before later plans add threshold assertions."
patterns-established:
  - "Code-context wrapper payloads compute `total_tokens` after cache/provenance metadata is attached."
  - "Gateway savings tests assert both cached and uncached provenance metadata for `code` operations."
requirements-completed: [FNDN-01]
duration: 24min
completed: 2026-05-18
---

# Phase 1 Plan 1: Retrieval Core & Routed Symbol Search Summary

**Retrieval cache, wrapper-aware budget packing, and a deterministic symbol-search smoke harness on the existing `code` surface**

## Performance

- **Duration:** 24 min
- **Started:** 2026-05-18T19:40:11Z
- **Completed:** 2026-05-18T20:03:39Z
- **Tasks:** 3
- **Files modified:** 10

## Accomplishments
- Landed the in-flight M0 cache and budget primitives inside `code_context` without adding a new MCP tool.
- Locked cache/provenance/budget behavior with focused core and gateway regressions on the existing `code` response shape.
- Added the missing `tests/benchmarks/code_intel/` smoke harness so later Phase 1 plans have a benchmark surface to extend.

## Task Commits

1. **Task 1: Harden M0 cache, budget packing, and metadata paths in place**
   - `9561241` (`test`) add failing wrapper-budget regression
   - `7772369` (`feat`) harden cache and budget packing
   - `9806a3f` (`refactor`) stabilize the core regression fixture
   - `c4193ff` (`fix`) satisfy strict mypy on the budget-fit helper
2. **Task 2: Lock brownfield M0 behavior with focused core and gateway regressions**
   - `ce0f615` (`test`) lock gateway cache and telemetry regressions
   - `2ae8636` (`refactor`) stabilize the gateway budget regression fixture
3. **Task 3: Create the missing benchmark harness landing zone for Wave 0**
   - `68cc8ae` (`feat`) add the code-intel symbol-search smoke harness

## Files Created/Modified
- `src/atelier/core/capabilities/code_context/budget.py` - shared token-budget packer for code-context payloads
- `src/atelier/core/capabilities/code_context/cache.py` - SQLite-backed retrieval cache keyed by args, repo, and index version
- `src/atelier/core/capabilities/code_context/engine.py` - wrapper-aware packing, cache helpers, and strict typing for code ops
- `tests/core/test_code_context.py` - core regressions for cache invalidation, provenance, and wrapper-budget fit
- `tests/gateway/test_p0_mcp_surfaces.py` - MCP boundary regressions for cache invalidation and budget metadata
- `tests/gateway/test_savings_api.py` - savings telemetry regressions for cached and uncached code-tool metadata
- `src/benchmarks/code_intel/symbol_search_bench.py` - deterministic two-call symbol-search benchmark runner
- `tests/benchmarks/code_intel/test_symbol_search_bench.py` - smoke tests for the new benchmark harness

## Decisions Made
- Used wrapper-inclusive budget fitting in `CodeContextEngine` so `total_tokens` reflects the final response envelope rather than just the packed item list.
- Kept Phase 1 benchmark scope deterministic and local by measuring repeated symbol search/cache behavior instead of introducing threshold-heavy performance claims in M0.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed wrapper metadata budget overshoot**
- **Found during:** Task 1 (Harden M0 cache, budget packing, and metadata paths in place)
- **Issue:** `tool_search` packed item rows to the requested budget, then added cache/provenance metadata afterward, which could push the final payload over `budget_tokens`.
- **Fix:** Added wrapper-aware budget fitting and exact `total_tokens` calculation in `CodeContextEngine`, then locked the behavior with core and gateway regressions.
- **Files modified:** `src/atelier/core/capabilities/code_context/engine.py`, `tests/core/test_code_context.py`, `tests/gateway/test_p0_mcp_surfaces.py`
- **Verification:** `uv run pytest tests/core/test_code_context.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_savings_api.py tests/benchmarks/code_intel/test_symbol_search_bench.py -q`
- **Committed in:** `7772369`, `9806a3f`, `2ae8636`

**2. [Rule 3 - Blocking] Fixed strict mypy typing on the new budget-fit callback**
- **Found during:** Plan wave verification
- **Issue:** The new helper returned `Any` through an untyped callback, which failed `make typecheck`.
- **Fix:** Replaced the loose callback type with a strict `Callable[[list[dict[str, Any]]], dict[str, Any]]`.
- **Files modified:** `src/atelier/core/capabilities/code_context/engine.py`
- **Verification:** `make lint && make typecheck`
- **Committed in:** `c4193ff`

---

**Total deviations:** 2 auto-fixed (1 bug, 1 blocking)
**Impact on plan:** Both fixes were required to satisfy the plan's budget and validation guarantees. No scope creep beyond the planned M0 surfaces.

## Issues Encountered
- The full `make test` suite exceeded the Bash tool wait window twice after passing more than half the repository tests. Targeted Phase 1 regressions, benchmark smoke, lint, and typecheck all completed successfully.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- `code_context` now exposes cache-aware, provenance-aware M0 behavior that Phase 1 Plan 02 can route through when SCIP support is added.
- The new benchmark landing zone under `tests/benchmarks/code_intel/` is ready for M1/M2 threshold extensions.

## Known Stubs
None.

## Self-Check: PASSED
- FOUND: `.planning/phases/01-retrieval-core-routed-symbol-search/01-01-SUMMARY.md`
- FOUND commits: `9561241`, `7772369`, `9806a3f`, `c4193ff`, `ce0f615`, `2ae8636`, `68cc8ae`
