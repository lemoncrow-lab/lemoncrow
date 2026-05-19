---
phase: 03-semantic-recall-relationship-navigation
plan: "02"
subsystem: memory
tags: [m7, recall-symbol, memory, benchmarks, mcp]
requires:
  - "03-01"
provides:
  - `memory op="recall_symbol"` on the existing MCP surface
  - symbol-linked recall bundle assembly outside `mcp_server.py`
  - deterministic benchmark and validation evidence for the M7 token gate
affects: [03-03, code-context, memory, validation]
tech-stack:
  added:
    - symbol recall capability seam
    - recall_symbol benchmark
  patterns:
    - thin gateway dispatch with helper-owned recall assembly
    - low-token default include sets with heavier opt-in evidence
key-files:
  created:
    - src/atelier/core/capabilities/archival_recall/symbol_recall.py
    - tests/core/capabilities/archival_recall/test_symbol_recall.py
    - src/benchmarks/code_intel/recall_symbol_bench.py
    - tests/benchmarks/code_intel/test_recall_symbol_bench.py
  modified:
    - src/atelier/gateway/adapters/mcp_server.py
    - tests/gateway/test_mcp_memory_tools.py
    - docs/agent-os/validation-matrix.md
    - .planning/phases/03-semantic-recall-relationship-navigation/03-VALIDATION.md
key-decisions:
  - "Land M7 on `memory op=\"recall_symbol\"` and keep `mcp_server.py` to dependency wiring plus immediate delegation."
  - "Treat `definition` plus typed `memory` items as the default low-token bundle, with traces, decisions, and tests added only by explicit include."
patterns-established:
  - "Symbol-linked recall resolves ambiguity through `CodeContextEngine` before fusing any memory or trace evidence."
  - "Recall benchmarks compare the shipped MCP bundle against both expanded includes and manual multi-call discovery."
requirements-completed: [DISC-04]
duration: 16min
completed: 2026-05-19
---

# Phase 3 Plan 2: Semantic Recall & Relationship Navigation Summary

**`memory op="recall_symbol"` now returns a definition-anchored symbol recall bundle with linked memory by default, heavier evidence on opt-in, and deterministic M7 benchmark evidence**

## Performance

- **Duration:** 16 min
- **Started:** 2026-05-19T09:09:00Z
- **Completed:** 2026-05-19T09:24:35Z
- **Tasks:** 3
- **Files modified:** 8

## Accomplishments

- Added a dedicated `SymbolRecallCapability` that resolves the symbol first, then fuses linked memory blocks/passages, traces, decision excerpts, and related tests while preserving the definition anchor.
- Extended the existing `memory` MCP tool with additive `op="recall_symbol"` wiring and kept the gateway thin by delegating all bundle assembly to the new helper.
- Added a deterministic recall benchmark plus validation/trace guidance proving the default bundle stays under budget and smaller than expanded/manual discovery paths.

## Task Commits

1. **Task 1: Build the symbol-linked recall bundle assembler outside the gateway**
   - `86c7e49` (`test`) add failing symbol recall helper coverage
   - `f540a6d` (`feat`) add symbol-linked recall bundle helper
2. **Task 2: Add additive `memory op="recall_symbol"` wiring on the existing MCP surface**
   - `d6bde79` (`test`) add failing MCP recall_symbol coverage
   - `ef2cb9f` (`feat`) wire recall_symbol through memory MCP surface
3. **Task 3: Add a dedicated recall benchmark and update the Phase 3 validation contract**
   - `b26cff3` (`feat`) add recall_symbol benchmark evidence

## Files Created/Modified

- `src/atelier/core/capabilities/archival_recall/symbol_recall.py` - symbol resolution, evidence fusion, boundary-aware filtering, and budget trimming for recall bundles
- `src/atelier/gateway/adapters/mcp_server.py` - additive `memory op="recall_symbol"` dispatch and dependency wiring only
- `tests/core/capabilities/archival_recall/test_symbol_recall.py` - default bundle, budget trimming, decision-boundary, and related-test coverage
- `tests/gateway/test_mcp_memory_tools.py` - MCP surface coverage for default/expanded includes and the no-new-tool contract
- `src/benchmarks/code_intel/recall_symbol_bench.py` - deterministic default-vs-expanded-vs-manual recall token benchmark
- `tests/benchmarks/code_intel/test_recall_symbol_bench.py` - benchmark serialization and token-ratio assertions
- `docs/agent-os/validation-matrix.md` - M7 validation command and trace evidence requirement
- `.planning/phases/03-semantic-recall-relationship-navigation/03-VALIDATION.md` - Phase 3 trace evidence note for `03-02`

## Decisions Made

- Kept M7 on the existing `memory` surface to match the grounded plan and avoid adding a parallel `code op="recall"` path.
- Used a typed `memory` list (`block` + `passage`) for the default payload so the low-token contract stays simple while still reusing the existing store surfaces.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- The trace store requires `init()` before direct benchmark/test seeding, so the M7 gateway helper and gateway test setup initialize it explicitly before recording traces.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Phase `03-03` can build caller/callee traversal on top of the same thin-gateway pattern used here.
- The M7 validation row, benchmark pattern, and recorded trace evidence are ready for future Phase 3 closeout checks.

## Self-Check: PASSED

- FOUND: `.planning/phases/03-semantic-recall-relationship-navigation/03-02-SUMMARY.md`
- FOUND commits: `86c7e49`, `f540a6d`, `d6bde79`, `ef2cb9f`, `b26cff3`
