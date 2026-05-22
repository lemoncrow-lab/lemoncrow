---
phase: 04-historical-code-intelligence
plan: "04"
subsystem: code-intel
tags: [blame, temporal-search, benchmarks, validation, m15]
requires:
  - phase: 04-02
    provides: deleted-history search and graveyard benchmark baseline
  - phase: 04-03
    provides: blame substrate and trusted SCIP freshness metadata
provides:
  - additive `code op="blame"` MCP dispatch with optional churn
  - explicit `index_stale` responses plus live repo temporal filtering
  - M15 blame benchmark, historical cost-discipline evidence, and recorded trace ownership
affects: [historical-code-intelligence, code-context, m15, validation]
tech-stack:
  added: []
  patterns:
    - engine stays orchestration-only by delegating blame and changed-file filtering into git-history seams
    - gateway extends the existing `code` tool additively with `op="blame"` and `include_churn`
    - M15 evidence reuses shipped `code` surfaces for both benchmark and cost-discipline coverage
key-files:
  created:
    - src/benchmarks/code_intel/blame_bench.py
    - tests/benchmarks/code_intel/test_blame_bench.py
    - .planning/phases/04-historical-code-intelligence/deferred-items.md
  modified:
    - src/atelier/core/capabilities/code_context/engine.py
    - src/atelier/infra/code_intel/git_history/adapter.py
    - src/atelier/gateway/adapters/mcp_server.py
    - tests/core/test_code_context.py
    - tests/gateway/test_mcp_tool_handlers.py
    - tests/gateway/test_p0_mcp_surfaces.py
    - src/benchmarks/code_intel/cost_discipline.py
    - tests/benchmarks/code_intel/test_cost_discipline.py
    - docs/agent-os/validation-matrix.md
key-decisions:
  - "Treat routed SCIP `index_sha` metadata as the authoritative stale-index gate for `code op=\"blame\"`, while local-only symbols fall back to current HEAD."
  - "Reuse shipped historical public surfaces in cost discipline by folding one deleted-history scenario and one blame scenario into the aggregate benchmark."
patterns-established:
  - "Live temporal repo search filters already-ranked hits through git-history changed-file membership instead of launching a separate search path."
  - "M15 trace closeout is recorded from the same validation payload documented in the validation matrix."
requirements-completed: [HIST-01, HIST-02]
duration: inline
completed: 2026-05-19
---

# Phase 4 Plan 04: Blame surface and temporal closeout Summary

**Additive `code op="blame"` plus live temporal repo filtering, backed by explicit stale-index handling and M15 benchmark/cost evidence.**

## Performance

- **Duration:** inline
- **Completed:** 2026-05-19T12:39:23Z
- **Tasks:** 3
- **Files modified:** 10

## Accomplishments

- Added `CodeContextEngine.tool_blame()` with explicit `index_stale` responses, routed blame orchestration, retrieval-cache reuse, and live repo `since` / `touched_by` filtering through git-history changed-file sets.
- Extended the existing MCP `code` tool additively with `op="blame"` and `include_churn`, while keeping gateway logic to immediate delegation only.
- Added deterministic blame benchmark coverage, extended historical cost-discipline coverage, updated the validation matrix, and recorded M15 trace `20260519T123857-gsd-executor-ca2ed203`.

## Task Commits

1. **Task 1: Add thin engine orchestration for blame and live temporal repo filtering**
   - `b5a2022` test(04-04): add failing engine blame orchestration tests
   - `3898800` feat(04-04): orchestrate blame and live temporal filtering
2. **Task 2: Add additive MCP blame wiring and public-surface regression coverage**
   - `2f755f7` test(04-04): add failing MCP blame surface tests
   - `14534e3` feat(04-04): add additive MCP blame dispatch
3. **Task 3: Add blame benchmark and cost-discipline evidence, then close M15 trace ownership**
   - `c63939c` feat(04-04): add M15 benchmark and cost evidence

Additional validation fixes:

- `695d66f` fix(04-04): restore lint compliance for wave4 artifacts
- `8fa8629` fix(phase-04): restore historical typing

**Plan metadata:** updated after verification rerun and Phase 4 type-gate fix

## Validation

- ✅ `uv run pytest tests/core/test_code_context.py -k "blame or churn or temporal or index_stale" -q`
- ✅ `uv run pytest tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py -q`
- ✅ `uv run pytest tests/benchmarks/code_intel/test_blame_bench.py tests/benchmarks/code_intel/test_cost_discipline.py -q`
- ✅ `uv run pytest tests/core/test_code_context.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py tests/benchmarks/code_intel/test_blame_bench.py tests/benchmarks/code_intel/test_cost_discipline.py -k "blame or churn or temporal or index_stale" -q`
- ✅ `make typecheck`
- ⚠️ Broad repo tests still fail on earlier-phase call-graph debt outside `04-04-PLAN.md`; details are tracked in `.planning/phases/04-historical-code-intelligence/deferred-items.md`.

## Decisions Made

- Used routed SCIP freshness metadata to fail blame calls explicitly with `index_stale` instead of guessing line ownership against mismatched HEAD content.
- Reused existing retrieval-cache discipline and public historical surfaces instead of inventing a separate benchmark-only helper path for M15 evidence.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking issue] Restored routed SCIP fixture coverage in gateway tests**
- **Found during:** Task 2 verification
- **Issue:** Existing gateway SCIP fixtures lacked the now-required `index_sha`, so routed tests silently fell back to local provenance.
- **Fix:** Added valid fixture freshness metadata in the gateway test helper so MCP surface tests continue exercising the routed SCIP path.
- **Files modified:** `tests/gateway/test_mcp_tool_handlers.py`
- **Commit:** `14534e3`

**2. [Rule 1 - Bug] Fixed lint regressions introduced by new imports**
- **Found during:** Repo-wide validation
- **Issue:** Ruff rejected import ordering in `engine.py` and the new blame benchmark.
- **Fix:** Reordered imports to restore lint compliance.
- **Files modified:** `src/atelier/core/capabilities/code_context/engine.py`, `src/benchmarks/code_intel/blame_bench.py`
- **Commit:** `695d66f`

## Deferred Issues

- Broad repo tests still report unrelated failures outside the Wave 4 target files; see `.planning/phases/04-historical-code-intelligence/deferred-items.md`.

## Known Stubs

None.

## Threat Flags

None.

## Self-Check

PASSED

---
*Phase: 04-historical-code-intelligence*
*Completed: 2026-05-19*
