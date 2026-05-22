---
phase: "02"
plan: "04"
subsystem: code-intel
tags: [usages, scip, treesitter, m3, benchmarks, m12]
requires:
  - "02-03"
provides:
  - `code op="usages"` on the existing MCP surface
  - routed SCIP-backed references with explicit treesitter fallback
  - usages benchmark evidence and final M12 follow-through closure
affects: [phase-02-closeout, code-context, scip, mcp]
tech-stack:
  added:
    - usages benchmark
  patterns:
    - routed symbol-intel providers extended for reference lookups
    - grouped budget-packed usage payloads on the existing `code` tool
key-files:
  created:
    - src/benchmarks/code_intel/usages_bench.py
    - tests/benchmarks/code_intel/test_usages_bench.py
  modified:
    - src/atelier/core/capabilities/code_context/models.py
    - src/atelier/core/capabilities/code_context/intel_store.py
    - src/atelier/core/capabilities/code_context/engine.py
    - src/atelier/gateway/adapters/mcp_server.py
    - src/atelier/infra/code_intel/scip/adapter.py
    - src/atelier/infra/code_intel/scip/reader.py
    - tests/core/test_code_context.py
    - tests/gateway/test_mcp_tool_handlers.py
    - tests/gateway/test_p0_mcp_surfaces.py
    - tests/infra/code_intel/scip/test_scip_adapter.py
    - docs/agent-os/validation-matrix.md
    - .planning/phases/02-structural-discovery-symbol-safe-change-flows/02-VALIDATION.md
key-decisions:
  - "Extend the existing routed symbol-intel seam with `find_references` instead of adding a new usages tool or side channel."
  - "Treat missing routed reference data as an explicit treesitter fallback, not a grep/text-search fallback."
patterns-established:
  - "Grouped usage payloads use the same budget-fitting machinery as search, outline, and pattern flows."
  - "SCIP artifacts can now carry optional reference payloads beside symbol payloads in the same trusted JSON fixture shape."
requirements-completed: [NAVG-02]
duration: inline
completed: 2026-05-19
---

# Phase 2 Plan 4: Structural Discovery & Symbol-Safe Change Flows Summary

**`code op="usages"` now ships on the existing MCP surface with routed SCIP references, explicit treesitter fallback, grouped low-token defaults, and the final benchmark evidence that fully closes M12**

## Performance

- **Duration:** inline continuation after `02-03`
- **Completed:** 2026-05-19
- **Tasks:** 3
- **Files modified:** 12

## Accomplishments

- Extended the routed code-intel seam with typed reference records, `find_references` provider/store hooks, and SCIP artifact support for persisted reference payloads.
- Added `code op="usages"` to the existing gateway/engine surface with grouped responses, explicit disambiguation, cache support, routed provenance, and truthful treesitter fallback when routed reference data is absent.
- Added a deterministic usages benchmark, updated the validation matrix, and recorded the final M3 trace that closes the remaining M12 follow-through checks.

## Task Commits

1. **Task 1 + Task 2: Add routed usages support and wire it through the existing `code` surface**
   - `2dd3327` (`feat`) add routed usages workflow
2. **Task 3: Add usages benchmark evidence and final validation guidance**
   - `4c4fdad` (`feat`) add usages benchmark evidence

## Files Created/Modified

- `src/atelier/core/capabilities/code_context/models.py` - typed usage/reference payload model
- `src/atelier/core/capabilities/code_context/intel_store.py` - routed `find_references` provider/store contract with explicit fallback
- `src/atelier/core/capabilities/code_context/engine.py` - cache-aware `tool_usages`, grouped budget packing, disambiguation handling, and shallow treesitter fallback
- `src/atelier/gateway/adapters/mcp_server.py` - additive `code op="usages"` dispatch on the existing MCP tool
- `src/atelier/infra/code_intel/scip/reader.py` and `adapter.py` - trusted reference payload parsing and routed provider support
- `tests/core/test_code_context.py`, `tests/gateway/test_mcp_tool_handlers.py`, `tests/gateway/test_p0_mcp_surfaces.py`, and `tests/infra/code_intel/scip/test_scip_adapter.py` - core, MCP, and routed-provider regressions for usages
- `src/benchmarks/code_intel/usages_bench.py` and `tests/benchmarks/code_intel/test_usages_bench.py` - deterministic usages-vs-grep token gate
- `docs/agent-os/validation-matrix.md` and `.planning/.../02-VALIDATION.md` - explicit M3 validation row and final M12 closeout language

## Decisions Made

- Kept usages on the existing `code` surface to preserve the grounded “extend existing MCP surfaces” rule.
- Returned treesitter provenance explicitly when routed reference data is missing so callers can distinguish shallow local fallback from SCIP-backed navigation.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Raised the benchmark request budget after the first usage benchmark could not fit the protected essentials**
- **Found during:** `tests/benchmarks/code_intel/test_usages_bench.py`
- **Issue:** a `budget_tokens=180` benchmark request was smaller than the minimum grouped usages payload needed to satisfy the frozen protected-top-rank contract, so the benchmark returned `budget_too_small` instead of a real usages payload.
- **Fix:** increased the benchmark request budget to `220`, which still stays well below the grep/read baseline while fitting the intended default usages response.
- **Files modified:** `src/benchmarks/code_intel/usages_bench.py`
- **Verification:** `TMPDIR=/home/pankaj/.copilot/session-state/46df9953-1e9a-4044-b4f7-894b5646ea13/tmp uv run pytest tests/benchmarks/code_intel/test_usages_bench.py -q`

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** No scope change. The adjustment preserved the intended low-token benchmark while keeping the request above the protected minimum.

## Issues Encountered

- The workspace `/tmp` mount is still full in this environment, so the usages tests and benchmarks continued to run with `TMPDIR` redirected into the session-state directory.

## User Setup Required

- None for automated plan closeout. Phase 2 still needs phase-level verification and any resulting human/UAT approval before the phase can be fully completed.

## Next Phase Readiness

- All four Phase 2 plans are now complete, and the phase is ready for verification/closeout.
- The routed usages seam and benchmark pattern now provide direct analogs for later semantic recall and relationship-navigation work in Phase 3.

## Known Stubs

- None within Plan `02-04`; remaining work is phase-level verification and closeout.

## Self-Check: PASSED

- FOUND: `.planning/phases/02-structural-discovery-symbol-safe-change-flows/02-04-SUMMARY.md`
- FOUND commits: `2dd3327`, `4c4fdad`
