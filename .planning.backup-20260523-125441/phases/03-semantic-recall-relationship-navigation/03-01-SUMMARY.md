---
phase: "03"
plan: "01"
subsystem: code-intel
tags: [semantic-search, hybrid-ranking, embeddings, m6, benchmarks, mcp]
requires:
  - "02-04"
provides:
  - additive semantic and hybrid ranking on `code op="search"`
  - exact-name-safe auto mode with mode-aware cache keys
  - deterministic M6 benchmark and trace-backed validation guidance
affects: [phase-03-closeout, code-context, mcp, benchmarks]
tech-stack:
  added:
    - local semantic ranking helper
    - semantic symbol search benchmark
  patterns:
    - keep ranking logic in `code_context` helpers while `engine.py` and `mcp_server.py` stay thin
    - widen retrieval cache keys by effective search mode for additive search behavior
key-files:
  created:
    - src/atelier/core/capabilities/code_context/embedding.py
  modified:
    - src/atelier/core/capabilities/code_context/engine.py
    - src/atelier/gateway/adapters/mcp_server.py
    - src/benchmarks/code_intel/symbol_search_bench.py
    - tests/core/test_code_context.py
    - tests/gateway/test_mcp_tool_handlers.py
    - tests/gateway/test_p0_mcp_surfaces.py
    - tests/benchmarks/code_intel/test_symbol_search_bench.py
    - docs/agent-os/validation-matrix.md
    - .planning/phases/03-semantic-recall-relationship-navigation/03-VALIDATION.md
key-decisions:
  - "Keep semantic and hybrid ranking on the existing `code op=\"search\"` surface with an additive `mode` parameter."
  - "Use a dedicated `embedding.py` helper with local cached embeddings so brownfield hotspots stay thin."
  - "Benchmark semantic quality through the existing benchmark landing zone and record M6 trace evidence against the milestone doc."
patterns-established:
  - "Auto mode resolves natural-language queries to hybrid search while exact identifier queries remain lexical."
  - "Semantic ranking reuses repo-local cached embeddings and reciprocal-rank fusion without adding new top-level MCP tools."
requirements-completed: [DISC-03]
duration: 28 min
completed: 2026-05-19
---

# Phase 3 Plan 1: Semantic Recall & Relationship Navigation Summary

**Hybrid semantic symbol search now ships on `code op="search"` with exact-name-safe auto mode, cached local embeddings, and deterministic M6 benchmark evidence.**

## Performance

- **Duration:** 28 min
- **Started:** 2026-05-19T08:38:06Z
- **Completed:** 2026-05-19T09:06:53Z
- **Tasks:** 3
- **Files modified:** 10

## Accomplishments

- Added a dedicated semantic ranking helper that resolves search mode, caches local embeddings, and fuses lexical plus semantic results without bloating `engine.py`.
- Wired additive `mode="auto" | "lexical" | "semantic" | "hybrid"` support through the existing `code` MCP tool while preserving exact identifier ordering for auto mode.
- Extended the existing symbol-search benchmark, validation matrix, and Phase 3 validation doc with M6 NDCG and exact-name regression gates, then recorded trace evidence tied to `M6-semantic-rank.md`.

## Task Commits

1. **Task 1: Add a dedicated semantic-ranking helper behind `tool_search`**
   - `f037f66` (`test`) failing engine coverage for semantic ranking, exact-name auto mode, and mode-aware cache keys
   - `c83e9c6` (`feat`) semantic ranking helper plus thin engine orchestration
2. **Task 2: Add additive MCP wiring for mode-aware symbol search**
   - `16ce1ef` (`test`) failing MCP coverage for additive `mode` wiring
   - `16524bb` (`feat`) thin gateway delegation for semantic and hybrid search modes
3. **Task 3: Extend the existing symbol-search benchmark and Phase 3 validation evidence**
   - `64b3345` (`test`) deterministic semantic benchmark and validation evidence
   - `7e27b94` (`fix`) lint/type cleanups for the semantic ranking helper under repo validation gates

## Files Created/Modified

- `src/atelier/core/capabilities/code_context/embedding.py` - search-mode resolution, embedding cache reuse, semantic scoring, and reciprocal-rank fusion
- `src/atelier/core/capabilities/code_context/engine.py` - thin orchestration for mode-aware search and mode-aware cache keys
- `src/atelier/gateway/adapters/mcp_server.py` - additive `mode` parameter on existing `code op="search"` dispatch
- `src/benchmarks/code_intel/symbol_search_bench.py` - semantic/hybrid fixture benchmark and exact-name regression checks on the existing benchmark seam
- `tests/core/test_code_context.py` - semantic ranking, auto-mode, and cache-key regression coverage
- `tests/gateway/test_mcp_tool_handlers.py` - thin gateway delegation coverage for `mode`
- `tests/gateway/test_p0_mcp_surfaces.py` - additive MCP contract coverage for semantic/hybrid search
- `tests/benchmarks/code_intel/test_symbol_search_bench.py` - M6 NDCG and exact-identifier benchmark gates
- `docs/agent-os/validation-matrix.md` - explicit M6 validation command and trace requirement
- `.planning/phases/03-semantic-recall-relationship-navigation/03-VALIDATION.md` - Phase 3 validation guidance updated for M6 trace evidence

## Decisions Made

- Kept semantic recall on the current `code op="search"` surface to follow the grounded “extend existing MCP surfaces” rule.
- Treated `mode="auto"` as a resolver for cheap exact-name queries first, only promoting natural-language queries to hybrid ranking.
- Reused the existing local embedder and vector cache helpers instead of adding a new dependency or remote embedding service.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Switched trace capture to `uv run python` so the Atelier package resolved correctly**
- **Found during:** Task 3 (benchmark and validation evidence)
- **Issue:** The initial trace-recording command used plain `python`, which could not import `atelier` from the repo environment.
- **Fix:** Re-ran the M6 trace capture through `uv run python` so the workspace virtualenv and package path were active.
- **Files modified:** None
- **Verification:** `uv run python - <<'PY' ... tool_record_trace(...) ... PY`
- **Committed in:** none (execution-only fix)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** No scope change. The fix only corrected the execution environment for the required M6 trace capture.

## Issues Encountered

- A full `make test` run exceeded the available verification window in this environment, so it was stopped after targeted Phase 03-01 pytest coverage plus `make lint` and `make typecheck` had already passed.

## User Setup Required

- None - no external service configuration required.

## Next Phase Readiness

- Phase 3 now has the semantic ranking primitive required for symbol-linked recall and caller/callee traversal follow-on work.
- The new benchmark and validation row give later Phase 3 plans a concrete pattern for trace-backed benchmark closeout on existing MCP surfaces.

## Known Stubs

- None.

## Self-Check: PASSED

- FOUND: `.planning/phases/03-semantic-recall-relationship-navigation/03-01-SUMMARY.md`
- FOUND: `src/atelier/core/capabilities/code_context/embedding.py`
- FOUND: `src/atelier/core/capabilities/code_context/engine.py`
- FOUND: `src/atelier/gateway/adapters/mcp_server.py`
- FOUND: `src/benchmarks/code_intel/symbol_search_bench.py`
- FOUND commits: `f037f66`, `c83e9c6`, `16ce1ef`, `16524bb`, `64b3345`, `7e27b94`
