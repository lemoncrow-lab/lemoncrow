---
phase: 03-semantic-recall-relationship-navigation
plan: "03"
subsystem: code-intel
tags: [m8, call-graph, scip, mcp, benchmarks, validation]
requires:
  - "03-02"
provides:
  - `code op="callers"` and `code op="callees"` on the existing MCP surface
  - routed SCIP call-edge traversal with explicit unavailable behavior
  - deterministic M8 benchmark and validation evidence for cheap default traversal
affects: [phase-03-closeout, code-context, scip, mcp, benchmarks]
tech-stack:
  added:
    - routed call-graph traversal helper
    - call-graph benchmark
  patterns:
    - thin gateway and engine wrappers over helper-owned traversal and snapshot shaping
    - typed routed SCIP call-edge payloads with explicit empty or unavailable responses
key-files:
  created:
    - src/atelier/core/capabilities/code_context/call_graph.py
    - src/benchmarks/code_intel/call_graph_bench.py
    - tests/benchmarks/code_intel/test_call_graph_bench.py
  modified:
    - src/atelier/core/capabilities/code_context/intel_store.py
    - src/atelier/core/capabilities/code_context/engine.py
    - src/atelier/gateway/adapters/mcp_server.py
    - src/atelier/infra/code_intel/scip/reader.py
    - src/atelier/infra/code_intel/scip/adapter.py
    - tests/core/test_code_context.py
    - tests/gateway/test_mcp_tool_handlers.py
    - tests/gateway/test_p0_mcp_surfaces.py
    - tests/infra/code_intel/scip/test_scip_adapter.py
    - docs/agent-os/validation-matrix.md
    - .planning/phases/03-semantic-recall-relationship-navigation/03-VALIDATION.md
key-decisions:
  - "Extend the existing routed provider contract with typed caller/callee neighbors instead of adding a new live fallback path."
  - "Keep traversal, cycle handling, and snapshot shaping in `call_graph.py` so `engine.py` and `mcp_server.py` stay thin."
  - "Ship `snapshot` as opt-in metadata for M8 while keeping the default `depth=1, snapshot=False` path cheap."
patterns-established:
  - "Call-graph queries resolve the symbol first, then traverse routed edges breadth-first with cycle-safe deduplication."
  - "Missing routed call-edge data returns a structured unavailable payload instead of silently switching to a guessed backend."
requirements-completed: [NAVG-03]
duration: 11 min
completed: 2026-05-19
---

# Phase 3 Plan 3: Semantic Recall & Relationship Navigation Summary

**Routed SCIP caller/callee traversal now ships on `code` with cheap depth-1 defaults, helper-owned graph walking, and deterministic M8 benchmark evidence.**

## Performance

- **Duration:** 11 min
- **Started:** 2026-05-19T09:32:06Z
- **Completed:** 2026-05-19T09:42:54Z
- **Tasks:** 3
- **Files modified:** 15

## Accomplishments

- Extended the routed SCIP artifact/provider seam with typed call-edge payloads for callers and callees.
- Added additive `code op="callers"` and `code op="callees"` wrappers with cycle-safe traversal, cheap defaults, and explicit unavailable responses.
- Added a deterministic call-graph benchmark plus validation-matrix and Phase 3 trace guidance for M8 closeout.

## Task Commits

1. **Task 1: Extend the routed SCIP contract with typed call-edge payloads**
   - `fb882c2` (`test`) add failing routed call graph provider coverage
   - `9dd2cb9` (`feat`) extend routed SCIP call edge contracts
2. **Task 2: Add thin engine and gateway traversal wrappers for `callers` / `callees`**
   - `5465683` (`test`) add failing callers and callees MCP coverage
   - `47c9a09` (`feat`) add callers and callees code tool wrappers
3. **Task 3: Add call-graph benchmark evidence and finalize Phase 3 validation coverage**
   - `f498c48` (`feat`) add call graph benchmark evidence

## Files Created/Modified

- `src/atelier/core/capabilities/code_context/call_graph.py` - typed neighbors, BFS traversal, cycle handling, response shaping, and snapshot metadata
- `src/atelier/core/capabilities/code_context/intel_store.py` - routed caller/callee provider contract and store delegation
- `src/atelier/core/capabilities/code_context/engine.py` - thin cached callers/callees wrappers and structured unavailable handling
- `src/atelier/gateway/adapters/mcp_server.py` - additive `code` tool dispatch for `callers` and `callees`
- `src/atelier/infra/code_intel/scip/reader.py` and `src/atelier/infra/code_intel/scip/adapter.py` - trusted call-edge payload parsing and routed provider exposure
- `tests/infra/code_intel/scip/test_scip_adapter.py` - routed call-edge loading, absence, and invalid-artifact coverage
- `tests/core/test_code_context.py`, `tests/gateway/test_mcp_tool_handlers.py`, and `tests/gateway/test_p0_mcp_surfaces.py` - traversal, snapshot, unavailable-path, and additive MCP coverage
- `src/benchmarks/code_intel/call_graph_bench.py` and `tests/benchmarks/code_intel/test_call_graph_bench.py` - deterministic default-vs-expanded call-graph token gate
- `docs/agent-os/validation-matrix.md` and `.planning/phases/03-semantic-recall-relationship-navigation/03-VALIDATION.md` - explicit M8 validation and trace requirements
- `.planning/phases/03-semantic-recall-relationship-navigation/deferred-items.md` - out-of-scope pre-existing full-typecheck failure noted during validation

## Decisions Made

- Kept relationship navigation on the existing `code` surface to match the grounded MCP extension rule.
- Used routed SCIP edge data only for M8 and returned explicit unavailable payloads when that data is absent.
- Limited snapshots to deterministic metadata in this plan so the default path stays cheap and the shared hotspots stay thin.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Fixed import-order and typing lint failures introduced by the new call-graph helper**
- **Found during:** Task 3 verification
- **Issue:** `make lint` failed on new imports in `call_graph.py`, `engine.py`, and the SCIP adapter after the M8 benchmark/docs changes landed.
- **Fix:** Switched `Callable` to `collections.abc` and normalized import ordering in the touched modules.
- **Files modified:** `src/atelier/core/capabilities/code_context/call_graph.py`, `src/atelier/core/capabilities/code_context/engine.py`, `src/atelier/infra/code_intel/scip/adapter.py`
- **Verification:** `make lint` and focused strict mypy on changed M8 files
- **Committed in:** `f498c48`

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** No scope change. The fix only restored repo lint compliance for the shipped M8 files.

## Issues Encountered

- Full `make typecheck` still reports a pre-existing unrelated strict mypy error in `src/atelier/core/capabilities/archival_recall/symbol_recall.py:309`; the plan's changed files passed focused strict mypy, and the unrelated failure was logged to `deferred-items.md`.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Phase 3 now has semantic search, symbol recall, and relationship navigation closed on existing MCP surfaces.
- Caller/callee traversal, benchmark evidence, and validation guidance are ready for Phase 3 closeout and later historical-navigation work.

## Self-Check: PASSED

- FOUND: `.planning/phases/03-semantic-recall-relationship-navigation/03-03-SUMMARY.md`
- FOUND commits: `fb882c2`, `9dd2cb9`, `5465683`, `47c9a09`, `f498c48`
