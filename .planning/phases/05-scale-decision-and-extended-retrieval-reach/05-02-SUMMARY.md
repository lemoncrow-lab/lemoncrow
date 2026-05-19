---
phase: 05-scale-decision-and-extended-retrieval-reach
plan: "02"
subsystem: infra
tags: [zoekt, search, benchmark, scale, m16]
requires:
  - phase: 05-01
    provides: search-only Zoekt decision and session-scoped lifecycle direction
provides:
  - session-scoped Zoekt runtime seam for large-repo text search
  - smart_search routing with additive backend and index_age_seconds metadata
  - M16 public-search benchmark evidence and recorded trace
affects: [Phase 06, search, benchmarks, validation]
tech-stack:
  added: [local Zoekt-compatible HTTP seam]
  patterns: [session-scoped backend supervisor, additive search metadata, warm-path benchmark validation]
key-files:
  created:
    - src/atelier/infra/code_intel/zoekt/__init__.py
    - src/atelier/infra/code_intel/zoekt/AGENT_README.md
    - src/atelier/infra/code_intel/zoekt/binary.py
    - src/atelier/infra/code_intel/zoekt/server.py
    - src/atelier/infra/code_intel/zoekt/client.py
    - src/atelier/infra/code_intel/zoekt/indexer.py
    - src/atelier/infra/code_intel/zoekt/adapter.py
    - tests/infra/code_intel/zoekt/test_zoekt_routing.py
    - src/benchmarks/code_intel/zoekt_bench.py
    - tests/benchmarks/code_intel/test_zoekt_bench.py
  modified:
    - src/atelier/core/capabilities/tool_supervision/search_read.py
    - src/atelier/core/capabilities/tool_supervision/smart_search.py
    - tests/gateway/test_p0_mcp_surfaces.py
    - docs/agent-os/validation-matrix.md
key-decisions:
  - "Keep Zoekt lifecycle ownership in a session-scoped supervisor outside CodeContextEngine and route only the existing smart_search stack."
  - "Expose backend and index_age_seconds additively while leaving code op=\"search\" semantics unchanged."
  - "Skip local reranking on routed Zoekt matches so the warm path keeps the M16 speed benefit."
patterns-established:
  - "Local search backends can use a per-workspace supervisor with health-gated fallback."
  - "Benchmark traces for shipped search paths must cite both milestone intent and budget-discipline context."
requirements-completed: [SCAL-01]
duration: inline
completed: 2026-05-19
---

# Phase 5 Plan 02: Validated large-repo backend routing for search workloads Summary

**Large-repo smart_search now routes through a session-scoped Zoekt backend with additive provenance metadata and a recorded M16 warm-path benchmark trace.**

## Performance

- **Duration:** inline
- **Started:** 2026-05-19T19:40:33Z
- **Completed:** 2026-05-19T19:56:07Z
- **Tasks:** 3
- **Files modified:** 14

## Accomplishments
- Added a pinned-binary Zoekt runtime seam with local-only health, lifecycle reuse, and byte-range-aware client payloads.
- Routed large-repo text search through the existing smart_search stack with fallback for small repos or unhealthy backend state.
- Added an M16 benchmark plus validation-matrix coverage and recorded trace `20260519T195519-gsd-executor-8d67f874`.

## Task Commits

Each task was committed atomically:

1. **Task 1: Build the Zoekt runtime seam for the approved `search`-workload path** - `f3a5823` (test), `390b075` (feat)
2. **Task 2: Route large-repo text search through Zoekt without widening code-intel hotspots** - `ae96018` (test), `8e4e51d` (feat)
3. **Task 3: Add the public-search scale benchmark and validation evidence** - `3d71881` (feat)

## Files Created/Modified
- `src/atelier/infra/code_intel/zoekt/binary.py` - resolves checksum-verified pinned binary paths for the local backend seam.
- `src/atelier/infra/code_intel/zoekt/server.py` - owns the local-only HTTP lifecycle and health endpoint reused per workspace.
- `src/atelier/infra/code_intel/zoekt/client.py` - maps backend responses into byte-range-preserving client matches.
- `src/atelier/infra/code_intel/zoekt/indexer.py` - builds the warm in-memory index used by routed large-repo searches.
- `src/atelier/infra/code_intel/zoekt/adapter.py` - adds threshold, health, and fallback routing for the search stack.
- `src/atelier/core/capabilities/tool_supervision/search_read.py` - serializes additive backend metadata and byte offsets.
- `src/atelier/core/capabilities/tool_supervision/smart_search.py` - routes large repos through Zoekt and preserves fallback behavior without touching engine.py or mcp_server.py.
- `tests/infra/code_intel/zoekt/test_zoekt_routing.py` - covers lifecycle reuse, health, fallback, metadata, and byte-range behavior.
- `tests/gateway/test_p0_mcp_surfaces.py` - guards additive public search metadata and unchanged code op="search" semantics.
- `src/benchmarks/code_intel/zoekt_bench.py` - benchmarks the shipped search surface and records the M16 trace.
- `tests/benchmarks/code_intel/test_zoekt_bench.py` - asserts the warm-path speed and budget gates.
- `docs/agent-os/validation-matrix.md` - adds the M16 validation row and trace command.

## Decisions Made
- Reused a session-scoped backend supervisor instead of tying lifecycle to per-call engine instances.
- Kept all public changes additive on the existing search stack and left `engine.py` plus `mcp_server.py` out of the implementation path.
- Recorded M16 evidence from the public search entry point and cited `docs/plans/active/code-intel/M16-zoekt-scale.md` with `docs/plans/active/code-intel/M12-token-budget.md`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Removed local reranking from routed Zoekt warm-path responses**
- **Found during:** Task 3 (Add the public-search scale benchmark and validation evidence)
- **Issue:** Routed Zoekt queries still paid the local FTS/semantic/graph reranking cost, which erased the required M16 warm-path speedup.
- **Fix:** Short-circuited routed Zoekt responses inside `smart_search.py` so the backend's ordered matches stay on the fast path while fallback search keeps the existing ranking behavior.
- **Files modified:** `src/atelier/core/capabilities/tool_supervision/smart_search.py`
- **Verification:** `uv run pytest tests/infra/code_intel/zoekt/test_zoekt_routing.py tests/gateway/test_p0_mcp_surfaces.py tests/benchmarks/code_intel/test_zoekt_bench.py -k "zoekt or backend or search" -q`
- **Committed in:** `3d71881`

---

**Total deviations:** 1 auto-fixed (1 missing critical)
**Impact on plan:** The fix was required to meet the plan's M16 performance gate. No hotspot scope creep was introduced.

## Issues Encountered
- The first benchmark run showed only ~1.14x speedup because routed Zoekt results were still paying local reranking cost; the warm-path optimization above resolved it.

## User Setup Required
None - no external service configuration required.

## Known Stubs
None.

## Next Phase Readiness
- Phase 5 is fully complete and Phase 6 can build on the search-backend supervisor and validation patterns established here.
- Operators still need a real pinned Zoekt binary or manifest entry outside tests for non-fixture runtime activation.

## Self-Check: PASSED
