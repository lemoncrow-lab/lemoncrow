---
phase: 06-bootstrap-dependency-scope-multi-repo-workspaces
plan: "02"
subsystem: infra
tags: [scip, external-scope, origin, symbol-edit, benchmark]
requires:
  - phase: 06-bootstrap-dependency-scope-multi-repo-workspaces
    provides: deterministic bootstrap warm-path and thin adapter discipline from 06-01
provides:
  - explicit `scope="external"` routing on existing code-intel search surfaces
  - typed `origin` metadata on routed symbol payloads for internal vs external artifacts
  - dependency symbol edit rejection with recorded M9 benchmark trace ownership
affects:
  - Phase 06-03 multi-repo workspace routing
  - Phase 07 maintainer validation guidance
tech-stack:
  added: []
  patterns:
    - repo-local `external-*.scip` discovery under the existing hashed SCIP cache root
    - typed `origin` metadata on `SymbolRecord`-shaped routed results
    - symbol-edit rejection before any file read for external dependency targets
key-files:
  created:
    - src/atelier/infra/code_intel/scip/external_artifacts.py
    - src/benchmarks/code_intel/external_scope_bench.py
    - tests/benchmarks/code_intel/test_external_scope_bench.py
  modified:
    - src/atelier/infra/code_intel/scip/indexer.py
    - src/atelier/infra/code_intel/scip/reader.py
    - src/atelier/infra/code_intel/scip/adapter.py
    - src/atelier/core/capabilities/code_context/models.py
    - src/atelier/core/capabilities/code_context/intel_store.py
    - src/atelier/core/capabilities/code_context/engine.py
    - src/atelier/core/capabilities/tool_supervision/symbol_edit.py
    - tests/infra/code_intel/scip/test_scip_adapter.py
    - tests/core/test_code_context.py
    - tests/gateway/test_mcp_tool_handlers.py
    - docs/agent-os/validation-matrix.md
key-decisions:
  - "Keep dependency search opt-in: `scope=\"repo\"` remains the default and `scope=\"external\"` routes only routed SCIP artifacts."
  - "Preserve the existing hashed `repo_id` cache identity and discover external artifacts by filename convention (`external-*.scip`) instead of adding live generation."
  - "Reject dependency symbol edits in `symbol_edit.py` before any filesystem read so external artifacts stay read-only and actionable."
patterns-established:
  - "SCIP artifact discovery returns typed internal/external descriptors, letting adapter routing stay thin while the trusted reader derives origin metadata."
  - "Gateway validation for external scope can stay on shipped `tool_code` and `tool_smart_edit` helpers without widening `mcp_server.py`."
requirements-completed: [DISC-05]
duration: 5m
completed: 2026-05-19
---

# Phase 6 Plan 2: External dependency scope and origin metadata Summary

**Explicit external dependency routing now discovers repo-local `external-*.scip` artifacts, tags symbol payloads with typed origin metadata, and rejects dependency symbol edits with recorded M9 trace evidence.**

## Performance

- **Duration:** 5m
- **Started:** 2026-05-19T22:58:49Z
- **Completed:** 2026-05-19T23:03:34Z
- **Tasks:** 3
- **Files modified:** 13

## Accomplishments
- Added repo-local external SCIP artifact discovery under the existing hashed cache root without adding live dependency indexing.
- Extended routed symbol payloads with additive `origin` metadata and kept default `scope="repo"` behavior free of dependency hits.
- Added external-scope benchmark coverage, validation-matrix guidance, and recorded M9 trace `20260519T230408-gsd-executor-a17abbc8`.

## Task Commits

Each task was committed atomically:

1. **Task 1: Discover external SCIP artifacts and tag routed symbols with origin metadata** - `000343d` (test), `ede3db0` (feat)
2. **Task 2: Route `scope="external"` explicitly and reject external symbol edits** - `4b4884b` (test), `ba32c80` (feat)
3. **Task 3: Add M9 scope-routing validation evidence and trace ownership** - `4158850` (feat)

**Plan metadata:** recorded in the final `docs(06-02)` metadata commit

_Note: TDD tasks used test → feat commits._

## Files Created/Modified
- `src/atelier/infra/code_intel/scip/external_artifacts.py` - Classifies repo-local SCIP artifacts as internal vs external using the existing cache root.
- `src/atelier/infra/code_intel/scip/indexer.py` - Discovers `external-*.scip` artifacts without changing hashed `repo_id` storage layout.
- `src/atelier/infra/code_intel/scip/reader.py` - Hydrates routed symbols with typed origin metadata through the trusted reader path.
- `src/atelier/infra/code_intel/scip/adapter.py` - Filters routed search hits by explicit repo vs external scope while preserving provider health behavior.
- `src/atelier/core/capabilities/code_context/models.py` - Adds typed `origin` to `SymbolRecord`.
- `src/atelier/core/capabilities/code_context/intel_store.py` - Routes explicit external searches without falling back to local repo results.
- `src/atelier/core/capabilities/code_context/engine.py` - Preserves repo-default behavior and keeps external scope routing additive on existing code ops.
- `src/atelier/core/capabilities/tool_supervision/symbol_edit.py` - Rejects dependency-origin symbol edits before any file mutation path.
- `tests/infra/code_intel/scip/test_scip_adapter.py` - Covers discovery, origin tagging, and malformed external artifact rejection.
- `tests/core/test_code_context.py` - Covers repo-default exclusion and explicit external-scope retrieval.
- `tests/gateway/test_mcp_tool_handlers.py` - Covers shipped `tool_code` / `tool_smart_edit` behavior for external scope and edit rejection.
- `src/benchmarks/code_intel/external_scope_bench.py` - Records M9 scope-routing and edit-rejection evidence with a trace id.
- `tests/benchmarks/code_intel/test_external_scope_bench.py` - Verifies benchmark serialization and dependency-edit rejection evidence.

## Decisions Made
- Kept dependency hits opt-in through `scope="external"` and did not add a union `"all"` scope.
- Preserved `origin` as typed metadata on symbol-shaped payloads so 06-03 can build on the same contract.
- Kept `mcp_server.py` out of the implementation path; engine/store/SCIP helpers own the new routing behavior.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- The JSON-RPC wrapper path in the targeted gateway suite hit an unrelated missing `atelier.core.capabilities.counterfactual` import, so gateway coverage for this plan stayed on direct `tool_code` / `tool_smart_edit` helper calls to keep 06-02 bounded to the shipped code-intel and edit surfaces.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Phase 06-03 can reuse the new `origin` contract and explicit external-vs-repo routing behavior when adding repo-aware workspace fan-out.
- External dependency artifacts now share the existing hashed cache identity, so multi-repo work can extend result metadata without migrating storage layout.

## Known Stubs

None.

## Self-Check: PASSED
