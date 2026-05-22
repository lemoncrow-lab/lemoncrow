---
phase: 06-bootstrap-dependency-scope-multi-repo-workspaces
plan: "01"
subsystem: infra
tags: [bootstrap, context, worker, memory, benchmark]
requires:
  - phase: 05-scale-decision-and-extended-retrieval-reach
    provides: thin adapter discipline for mcp_server.py and runtime hotspots
provides:
  - deterministic repo-scoped bootstrap block planning and pinned memory persistence
  - context-triggered bootstrap job enqueue and worker execution on the existing path
  - warmed-context benchmark evidence with recorded M11 trace ownership
affects:
  - Phase 06-02 external dependency scope routing
  - Phase 06-03 multi-repo workspace routing
tech-stack:
  added: []
  patterns:
    - repo-scoped pinned memory blocks under bootstrap/<repo_id>/...
    - async bootstrap enqueue on context plus worker reuse
key-files:
  created:
    - src/atelier/core/service/bootstrap_context.py
    - tests/core/service/test_bootstrap_context.py
    - src/benchmarks/code_intel/bootstrap_prefetch_bench.py
    - tests/benchmarks/code_intel/test_bootstrap_prefetch_bench.py
  modified:
    - src/atelier/core/service/jobs.py
    - src/atelier/core/service/worker.py
    - src/atelier/core/runtime/engine.py
    - src/atelier/gateway/adapters/mcp_server.py
    - tests/gateway/test_mcp_tool_handlers.py
    - docs/agent-os/validation-matrix.md
key-decisions:
  - "Persist bootstrap blocks under a repo-scoped bootstrap:<repo_id> memory namespace while keeping canonical bootstrap/<repo_id>/... labels."
  - "Keep first-context bootstrap fully implicit: tool_get_context enqueues one deduped bootstrap_context job and later sessions read persisted blocks through runtime injection."
patterns-established:
  - "Bootstrap planning lives in bootstrap_context.py; mcp_server.py only computes enqueue state and delegates."
  - "Later-session reuse happens by reading persisted bootstrap blocks during runtime context assembly instead of recomputing summaries."
requirements-completed: [ENBL-01]
duration: 11m
completed: 2026-05-19
---

# Phase 6 Plan 1: First-context bootstrap and pinned memory prefetch pipeline Summary

**Deterministic repo-scoped bootstrap blocks now warm later `context` sessions through the existing worker path with recorded M11 benchmark evidence.**

## Performance

- **Duration:** 11m
- **Started:** 2026-05-19T22:37:51Z
- **Completed:** 2026-05-19T22:49:47Z
- **Tasks:** 3
- **Files modified:** 10

## Accomplishments
- Added a deterministic bootstrap planner that writes pinned `bootstrap/<repo_id>/...` memory blocks without any LLM-generated summaries.
- Wired `tool_get_context` to enqueue one repo-scoped bootstrap job on the existing worker path and inject warmed bootstrap blocks into later context responses.
- Added an M11 benchmark smoke plus validation-matrix coverage and recorded trace `20260519T225500-gsd-executor-7d0b2661`.

## Task Commits

Each task was committed atomically:

1. **Task 1: Build the deterministic bootstrap payload writer** - `3dcacca` (test), `7b394d2` (feat)
2. **Task 2: Enqueue and execute bootstrap work on the existing context plus worker path** - `a2dc98f` (test), `a8e729f` (feat)
3. **Task 3: Add M11 warm-path validation evidence and trace ownership** - `8a90a38` (feat), `e2f450c` (fix)

**Plan metadata:** recorded in the final `docs(06-01)` metadata commit

_Note: TDD tasks used test → feat commits._

## Files Created/Modified
- `src/atelier/core/service/bootstrap_context.py` - Owns deterministic bootstrap block planning, persistence, and warm-context rendering.
- `src/atelier/core/service/jobs.py` - Registers the explicit `bootstrap_context` job type.
- `src/atelier/core/service/worker.py` - Dispatches bootstrap jobs through the existing worker registry.
- `src/atelier/core/runtime/engine.py` - Injects persisted bootstrap blocks into later context assembly.
- `src/atelier/gateway/adapters/mcp_server.py` - Adds thin enqueue/dedupe logic on `tool_get_context`.
- `tests/core/service/test_bootstrap_context.py` - Covers deterministic planning, persistence, and partial retry metadata.
- `tests/gateway/test_mcp_tool_handlers.py` - Covers enqueue dedupe, worker execution, and warmed-session reuse.
- `src/benchmarks/code_intel/bootstrap_prefetch_bench.py` - Records the M11 cold-vs-warm benchmark trace on the shipped context flow.
- `tests/benchmarks/code_intel/test_bootstrap_prefetch_bench.py` - Verifies benchmark serialization and warm-path reuse.
- `docs/agent-os/validation-matrix.md` - Adds the M11 validation gate and trace capture command.

## Decisions Made
- Used repo-scoped pinned memory blocks with stable `bootstrap/<repo_id>/...` labels so later sessions can reuse warmed context without session-specific agent IDs.
- Kept bootstrap async and implicit on the existing `context` path; no new MCP tool or manual bootstrap flag was introduced.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Redirected default benchmark output away from the repo root**
- **Found during:** Task 3 (Add M11 warm-path validation evidence and trace ownership)
- **Issue:** `run_bootstrap_prefetch_bench()` wrote a generated `code_intel_bootstrap_prefetch/` directory into the repo when called without an explicit work dir.
- **Fix:** Changed the default benchmark workspace to respect `TMPDIR` before falling back to the current directory, then re-ran the benchmark trace.
- **Files modified:** `src/benchmarks/code_intel/bootstrap_prefetch_bench.py`
- **Verification:** `uv run pytest tests/benchmarks/code_intel/test_bootstrap_prefetch_bench.py -q` and `uv run python -c "from benchmarks.code_intel.bootstrap_prefetch_bench import run_bootstrap_prefetch_bench; result = run_bootstrap_prefetch_bench(); print(result.trace_id)"`
- **Committed in:** `e2f450c`

---

**Total deviations:** 1 auto-fixed (1 Rule 1 bug)
**Impact on plan:** The fix kept the benchmark deterministic and prevented generated artifacts from polluting the repo worktree.

## Issues Encountered
- JSON-RPC handler tests exercised the mocked remote `context` client path, so bootstrap-specific gateway coverage switched to direct `tool_get_context()` wrapper calls to validate the local enqueue path.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Phase 06-02 can reuse the repo-scoped bootstrap memory pattern and thin-adapter enqueue discipline established here.
- The warmed context path is now benchmarked and trace-owned, so later Phase 6 plans can extend scope without reworking the bootstrap seam.

## Known Stubs

None.

## Self-Check: PASSED
