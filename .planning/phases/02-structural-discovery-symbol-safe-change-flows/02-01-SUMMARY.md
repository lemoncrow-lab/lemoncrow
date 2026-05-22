---
phase: "02"
plan: "01"
subsystem: code-intel
tags: [ast-grep, pattern-search, pattern-rewrite, benchmarks, mcp]
requires: []
provides:
  - `code op="pattern"` on the existing `code` MCP surface
  - explicit ast-grep availability path with managed bootstrap fallback
  - deterministic structural pattern benchmark coverage
affects: [02-02, 02-03, 02-04, code-context]
tech-stack:
  added:
    - repo-local ast-grep discovery/bootstrap support
  patterns:
    - thin `tool_code()` op branch with engine delegation
    - SCIP-shaped infra package layout for ast-grep
    - deterministic code-intel benchmark fixture pattern
key-files:
  created:
    - src/atelier/infra/code_intel/astgrep/__init__.py
    - src/atelier/infra/code_intel/astgrep/binaries.py
    - src/atelier/infra/code_intel/astgrep/adapter.py
    - src/atelier/infra/code_intel/astgrep/rewrite.py
    - src/benchmarks/code_intel/pattern_bench.py
    - tests/infra/code_intel/astgrep/test_astgrep_adapter.py
    - tests/benchmarks/code_intel/test_pattern_bench.py
  modified:
    - src/atelier/core/capabilities/code_context/engine.py
    - src/atelier/gateway/adapters/mcp_server.py
    - tests/gateway/test_mcp_tool_handlers.py
    - tests/gateway/test_p0_mcp_surfaces.py
    - docs/agent-os/validation-matrix.md
key-decisions:
  - "Pattern search stays on the existing `code` tool and delegates immediately into `CodeContextEngine`."
  - "ast-grep availability resolves through env override, exact binary discovery, then pinned managed bootstrap before returning `tool_unavailable`."
patterns-established:
  - "New code-intel ops reuse Phase 1 cache/provenance/token metadata instead of inventing a parallel payload shape."
  - "Structural pattern benchmarks reuse the existing fixture-backed code-intel benchmark stack."
requirements-completed: [DISC-02]
duration: 55min
completed: 2026-05-19
---

# Phase 2 Plan 1: Structural Discovery & Symbol-Safe Change Flows Summary

**Structural pattern search and rewrite now run through `code op="pattern"` on the existing MCP surface, backed by an ast-grep infra seam, managed availability flow, and benchmark coverage**

## Performance

- **Duration:** 55 min
- **Started:** 2026-05-19T00:29:57+02:00
- **Completed:** 2026-05-19T01:24:39+02:00
- **Tasks:** 3
- **Files modified:** 12

## Accomplishments

- Added a new `infra/code_intel/astgrep/` package with explicit binary discovery, managed bootstrap fallback, typed pattern match parsing, and rewrite support.
- Routed `code op="pattern"` through a thin `tool_code()` branch into a cache-aware engine wrapper without adding a new top-level MCP tool.
- Added deterministic structural pattern benchmarks and validation-matrix coverage for the M5 token gate.

## Task Commits

1. **Task 1: Build the ast-grep infra seam with explicit binary resolution and typed match parsing**
   - `8c2f2a4` (`test`) add failing ast-grep adapter coverage
   - `7903f6f` (`feat`) add ast-grep infra adapter seam
2. **Task 2: Add `code op="pattern"` through a thin gateway branch and cache-aware engine wrapper**
   - `3787d80` (`test`) add failing code pattern gateway coverage
   - `4123d83` (`feat`) route code pattern requests through engine
3. **Task 3: Add structural-pattern benchmarks, validation rows, and trace expectations**
   - `469f939` (`feat`) add structural pattern benchmark coverage
   - `f380afd` (`fix`) satisfy ast-grep validation gates

## Files Created/Modified

- `src/atelier/infra/code_intel/astgrep/binaries.py` - explicit binary resolution plus pinned managed bootstrap/download flow
- `src/atelier/infra/code_intel/astgrep/adapter.py` - typed ast-grep search/rewrite adapter and structured unavailability contract
- `src/atelier/infra/code_intel/astgrep/rewrite.py` - dry-run and apply rewrite helpers that report changed files
- `src/atelier/core/capabilities/code_context/engine.py` - cache-aware `tool_pattern` wrapper and reindex handoff
- `src/atelier/gateway/adapters/mcp_server.py` - additive `code op="pattern"` dispatch on the existing MCP tool
- `src/benchmarks/code_intel/pattern_bench.py` - deterministic structural-pattern benchmark runner
- `tests/infra/code_intel/astgrep/test_astgrep_adapter.py` - missing-binary, search, and rewrite regressions
- `tests/gateway/test_mcp_tool_handlers.py` and `tests/gateway/test_p0_mcp_surfaces.py` - MCP boundary coverage for `code op="pattern"`
- `tests/benchmarks/code_intel/test_pattern_bench.py` - structural pattern token-gate coverage
- `docs/agent-os/validation-matrix.md` - M5 validation row and real-machine binary verification expectation

## Decisions Made

- Kept structural pattern search on the existing `code` surface so Phase 2 honors the grounded “no new top-level tool” rule.
- Chose a concrete availability path for ast-grep instead of a failure-only contract, so `DISC-02` is deliverable on real machines instead of remaining an optional capability.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Finished strict validation cleanup after executor timeout**
- **Found during:** manual closeout after the executor timed out before writing the summary
- **Issue:** the main implementation commits landed, but final validation left small cleanup changes in ast-grep exports/imports and benchmark typing that were not yet committed.
- **Fix:** removed stale imports, tightened the managed-download typing, and committed the cleanup as `f380afd` so the targeted suite, lint, and typecheck all pass.
- **Files modified:** `src/atelier/core/capabilities/code_context/engine.py`, `src/atelier/infra/code_intel/astgrep/__init__.py`, `src/atelier/infra/code_intel/astgrep/adapter.py`, `src/atelier/infra/code_intel/astgrep/binaries.py`, `src/benchmarks/code_intel/pattern_bench.py`
- **Verification:** `uv run pytest tests/core/test_code_context.py tests/infra/code_intel/astgrep/test_astgrep_adapter.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py tests/benchmarks/code_intel/test_pattern_bench.py -q` and `make lint && make typecheck`

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** No scope creep. The manual closeout only finished validation cleanup and documentation after the executor timed out.

## Issues Encountered

- The executor timed out before it could write `02-01-SUMMARY.md` or finish the last cleanup commit, so the plan was closed out manually via commit/summary spot-check recovery.

## User Setup Required

- Real-machine follow-up from `02-VALIDATION.md`: verify the chosen ast-grep binary discovery/bootstrap path on the actual development machine before phase sign-off.

## Next Phase Readiness

- Phase 2 now has a working structural pattern surface that M12 can harden in `02-02`.
- The benchmark landing zone is ready for later Phase 2 cost-discipline, symbol-edit, and usages benchmarks.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: `.planning/phases/02-structural-discovery-symbol-safe-change-flows/02-01-SUMMARY.md`
- FOUND commits: `8c2f2a4`, `7903f6f`, `3787d80`, `4123d83`, `469f939`, `f380afd`
