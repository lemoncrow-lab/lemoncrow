---
phase: "02"
plan: "03"
subsystem: tool-supervision
tags: [symbol-edit, rich-edit, m4, benchmarks, mcp]
requires:
  - "02-02"
provides:
  - `edit` support for `kind="symbol"` on the existing MCP surface
  - explicit stale-target and ambiguity guards for symbol edits
  - deterministic symbol-edit benchmark and M4 trace evidence
affects: [02-04, code-context, tool-supervision]
tech-stack:
  added:
    - symbol edit planner seam
    - symbol edit benchmark
  patterns:
    - thin gateway snapshot/diff shell with core symbol resolution
    - fixture-backed code-intel benchmark for edit token gates
key-files:
  created:
    - src/atelier/core/capabilities/tool_supervision/symbol_edit.py
    - src/benchmarks/code_intel/symbol_edit_bench.py
    - tests/core/capabilities/tool_supervision/test_rich_edit_symbol.py
    - tests/benchmarks/code_intel/test_symbol_edit_bench.py
  modified:
    - src/atelier/core/capabilities/tool_supervision/rich_edit.py
    - src/atelier/gateway/adapters/mcp_server.py
    - tests/gateway/test_mcp_jsonrpc_e2e.py
    - docs/agent-os/validation-matrix.md
    - .planning/phases/02-structural-discovery-symbol-safe-change-flows/02-VALIDATION.md
key-decisions:
  - "Keep symbol edits on the existing `edit` tool and route all symbol resolution through a dedicated core seam."
  - "Close the edit-side M12 follow-through in 02-03, but leave final M12 completion blocked on usages in 02-04."
patterns-established:
  - "Symbol-targeted edits reuse the existing atomic rich-edit write path instead of introducing a separate edit surface."
  - "Code-intel edit benchmarks compare compact rich descriptors against search/read plus line-edit baselines."
requirements-completed: [DISC-01]
duration: inline
completed: 2026-05-19
---

# Phase 2 Plan 3: Structural Discovery & Symbol-Safe Change Flows Summary

**Named-symbol edits now ship on the existing `edit` MCP surface, with safe ambiguity/staleness guards, reindex-and-memory follow-through, and deterministic benchmark evidence for the M4 token gate**

## Performance

- **Duration:** inline continuation after `02-02`
- **Completed:** 2026-05-19
- **Tasks:** 3
- **Files modified:** 9

## Accomplishments

- Added a dedicated `symbol_edit.py` core seam that resolves symbol descriptors through `CodeContextEngine`, rejects ambiguous or stale targets, and plans concrete file writes without pushing code-intel logic into the gateway.
- Routed `kind="symbol"` descriptors through the existing atomic rich-edit path so diff recording, rollback behavior, reindexing, and memory tagging stay on the current `edit` tool.
- Added a deterministic symbol-edit benchmark, updated the validation contracts, and recorded M4 trace evidence while keeping final M12 closure blocked on `02-04`.

## Task Commits

1. **Task 1 + Task 2: Add the symbol-edit core seam and wire it through the existing `edit` flow**
   - `c263ead` (`feat`) add symbol edit seam
2. **Task 3: Add benchmark evidence and validation guidance for the M4 token gate**
   - `3e33062` (`feat`) add symbol edit benchmark evidence

## Files Created/Modified

- `src/atelier/core/capabilities/tool_supervision/symbol_edit.py` - symbol resolution, stale-target guards, concrete replacement planning, and memory tagging
- `src/atelier/core/capabilities/tool_supervision/rich_edit.py` - `kind="symbol"` dispatch through the existing atomic file-write path
- `src/atelier/gateway/adapters/mcp_server.py` - symbol-aware touched-path snapshotting so diff recording follows the real edited file
- `tests/core/capabilities/tool_supervision/test_rich_edit_symbol.py` - ambiguity, stale target, reindex, and memory-tag regressions
- `tests/gateway/test_mcp_jsonrpc_e2e.py` - end-to-end MCP coverage for symbol descriptors on `edit`
- `src/benchmarks/code_intel/symbol_edit_bench.py` - deterministic benchmark comparing symbol-edit tokens against a search/read plus line-edit baseline
- `tests/benchmarks/code_intel/test_symbol_edit_bench.py` - `<=30% of baseline` token-gate coverage
- `docs/agent-os/validation-matrix.md` and `.planning/.../02-VALIDATION.md` - explicit M4 validation row, trace requirement, and edit-side M12 follow-through closeout

## Decisions Made

- Kept symbol edits on the existing `edit` tool to preserve the grounded “extend existing MCP surfaces” rule.
- Used the file’s exact line-span text for rich-edit replacement instead of the dedented `get_symbol().source` payload so symbol replacements preserve correct scope and reindex cleanly.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Reconstructed the exact file-scoped symbol span after the first implementation broke method indentation**
- **Found during:** `test_symbol_edit_reindexes_and_tags_memory_on_success`
- **Issue:** `CodeContextEngine.get_symbol()` returns a dedented header on the first line, so the initial replacement path mis-indented the rewritten method and dropped it out of the class scope.
- **Fix:** planned symbol edits now derive the real line-span text from the file for `old_string`/`new_string`, while using the indexed payload only for stale-target checks.
- **Files modified:** `src/atelier/core/capabilities/tool_supervision/symbol_edit.py`
- **Verification:** `TMPDIR=/home/pankaj/.copilot/session-state/46df9953-1e9a-4044-b4f7-894b5646ea13/tmp uv run pytest tests/core/capabilities/tool_supervision/test_rich_edit_symbol.py tests/gateway/test_mcp_jsonrpc_e2e.py::test_symbol_edit_descriptor_e2e tests/benchmarks/code_intel/test_symbol_edit_bench.py -q`

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** No scope change. The fix tightened the implementation to match the planned safe symbol-edit contract.

## Issues Encountered

- The workspace `/tmp` mount is full in this environment, so symbol-edit tests and benchmarks needed `TMPDIR` redirected into the session-state directory to avoid sqlite/tempfile failures.

## User Setup Required

- None for automated closeout. Phase 2 still needs the practical symbol-first edit plus usages workflow from `02-VALIDATION.md` before phase sign-off.

## Next Phase Readiness

- `02-04` is the only remaining Phase 2 plan and now owns the final usages surface plus the last open M12 follow-through checks.
- The symbol-edit seam, benchmark pattern, and validation row are now ready to serve as direct analogs for the usages implementation.

## Known Stubs

- Full M12 closure is still intentionally deferred until `02-04`.

## Self-Check: PASSED

- FOUND: `.planning/phases/02-structural-discovery-symbol-safe-change-flows/02-03-SUMMARY.md`
- FOUND commits: `c263ead`, `3e33062`
