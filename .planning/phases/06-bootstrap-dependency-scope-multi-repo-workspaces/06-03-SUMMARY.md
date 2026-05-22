---
phase: 06-bootstrap-dependency-scope-multi-repo-workspaces
plan: "03"
subsystem: infra
tags: [workspace, code-intel, repo-filter, repo-name, benchmark]
requires:
  - phase: 06-bootstrap-dependency-scope-multi-repo-workspaces
    provides: first-context bootstrap warm path and thin adapter discipline from 06-01
  - phase: 06-bootstrap-dependency-scope-multi-repo-workspaces
    provides: external dependency `origin` metadata and read-only edit rejection from 06-02
provides:
  - validated `.atelier/workspace.toml` parsing for multi-repo code-intel workspaces
  - additive `repo` routing for workspace-backed `code` search and symbol operations
  - additive `repo_name` metadata on merged workspace results with recorded M10 benchmark trace ownership
affects:
  - Phase 07 maintainer playbooks and scorecards
  - multi-repo code-intel validation guidance
tech-stack:
  added: []
  patterns:
    - repo-root-relative workspace config parsing with sibling-repo validation
    - helper-owned workspace fan-out above per-repo CodeContextEngine instances
    - additive repo-aware metadata on existing `tool_code` payloads without changing hashed `repo_id`
key-files:
  created:
    - src/atelier/core/capabilities/code_context/workspace_config.py
    - src/atelier/core/capabilities/code_context/workspace_router.py
    - src/benchmarks/code_intel/workspace_bench.py
    - tests/core/test_code_context_workspace.py
    - tests/benchmarks/code_intel/test_workspace_bench.py
  modified:
    - src/atelier/core/capabilities/code_context/models.py
    - src/atelier/gateway/adapters/mcp_server.py
    - tests/gateway/test_mcp_tool_handlers.py
    - tests/gateway/test_p0_mcp_surfaces.py
    - docs/agent-os/validation-matrix.md
key-decisions:
  - "Keep multi-repo support in a workspace parser/router helper layer instead of making `CodeContextEngine` multi-root."
  - "Route only workspace-backed `code op=search` and `code op=symbol`; unsupported ops reject additive `repo` filters instead of widening behavior."
  - "Preserve hashed `repo_id` storage identity and add human-facing `repo_name` metadata only on merged or filtered workspace payloads."
patterns-established:
  - "Workspace config resolves repo paths relative to the active repo and validates sibling targets before routing."
  - "Gateway delegation checks for workspace config, enforces repo-filter scope, and hands off fan-out work to helper modules."
requirements-completed: [NAVG-04]
duration: 5m
completed: 2026-05-19
---

# Phase 6 Plan 3: Multi-repo workspace routing and repo-aware result handling Summary

**Workspace-backed `code` search and symbol routing now fan out across configured sibling repos, preserve hashed `repo_id` identity, and return additive `repo_name` metadata with recorded M10 benchmark evidence.**

## Performance

- **Duration:** 5m
- **Started:** 2026-05-19T23:08:17Z
- **Completed:** 2026-05-19T23:13:25Z
- **Tasks:** 3
- **Files modified:** 10

## Accomplishments
- Added `.atelier/workspace.toml` parsing plus validation for sibling repo routing without changing `CodeContextEngine` ownership.
- Routed workspace `code` search and symbol calls through a helper layer with additive `repo` filtering and additive `repo_name` metadata while preserving 06-02 `origin` tags.
- Added benchmark smoke coverage, validation-matrix guidance, and recorded M10 trace `20260519T231246-gsd-executor-5b6e9698`.

## Task Commits

Each task was committed atomically:

1. **Task 1: Define workspace config and per-repo routing contracts** - `9e7a8c8` (test), `e5ab19d` (feat)
2. **Task 2: Add additive `repo` routing and repo-aware result metadata on `tool_code`** - `e1a816b` (test), `013e47d` (feat)
3. **Task 3: Add M10 workspace validation evidence and trace ownership** - `f9da91d` (feat)

**Plan metadata:** pending final docs commit

_Note: TDD tasks used test → feat commits._

## Files Created/Modified
- `src/atelier/core/capabilities/code_context/workspace_config.py` - Parses and validates workspace repo definitions from `.atelier/workspace.toml`.
- `src/atelier/core/capabilities/code_context/workspace_router.py` - Fans out supported read-only code ops across configured repos and annotates results with `repo_name`.
- `src/atelier/core/capabilities/code_context/models.py` - Extends symbol and usage records with additive `repo_name` metadata.
- `src/atelier/gateway/adapters/mcp_server.py` - Adds the additive `repo` parameter and delegates supported workspace routing while keeping unsupported ops bounded.
- `tests/core/test_code_context_workspace.py` - Covers workspace config parsing, repo filtering, repo-name tagging, and deterministic symbol selection.
- `tests/gateway/test_mcp_tool_handlers.py` - Covers shipped workspace search/symbol behavior and preserved external `origin` metadata.
- `tests/gateway/test_p0_mcp_surfaces.py` - Covers MCP schema exposure for `repo` and clear rejection for unsupported repo-filtered ops.
- `src/benchmarks/code_intel/workspace_bench.py` - Records M10 union/filter benchmark evidence and trace ownership.
- `tests/benchmarks/code_intel/test_workspace_bench.py` - Verifies benchmark serialization and workspace routing evidence.
- `docs/agent-os/validation-matrix.md` - Adds the M10 validation row and trace command for workspace routing.

## Decisions Made
- Kept workspace fan-out above the per-repo engine boundary so hashed `repo_id` storage and cache identity remain unchanged.
- Limited cross-repo behavior to `code op="search"` and `code op="symbol"` in this phase; additive `repo` filters on unsupported ops now fail clearly.
- Preserved 06-02 `origin` metadata and layered `repo_name` beside it so callers can disambiguate merged workspace hits safely.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed repeated workspace test setup collisions**
- **Found during:** Task 1 (Define workspace config and per-repo routing contracts)
- **Issue:** The new workspace test helper recreated a sibling fixture directory without `exist_ok=True`, causing repeated test setup failures.
- **Fix:** Allowed idempotent fixture directory creation in the workspace test helper.
- **Files modified:** `tests/core/test_code_context_workspace.py`
- **Verification:** `uv run pytest tests/core/test_code_context_workspace.py -q`
- **Committed in:** `e5ab19d` (part of Task 1 commit)

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** The auto-fix was test-only and kept execution aligned with the planned workspace scope.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Phase 6 now closes with bootstrap, dependency scope, and workspace routing evidence all recorded on shipped tool surfaces.
- Phase 7 can reference the new workspace validation row and repo-aware payload examples when writing maintainer playbooks and scorecards.

## Known Stubs

None.

## Self-Check: PASSED
