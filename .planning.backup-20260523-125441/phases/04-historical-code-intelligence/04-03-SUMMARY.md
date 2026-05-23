---
phase: 04-historical-code-intelligence
plan: "03"
subsystem: infra
tags: [pygit2, scip, blame, churn, freshness]
requires:
  - phase: 04-02
    provides: deleted-history git substrate and routed SCIP provider baseline
provides:
  - typed blame/churn annotations under git_history/
  - explicit SCIP index_sha freshness propagation for routed symbol payloads
affects: [historical-code-intelligence, code-context, m15]
tech-stack:
  added: []
  patterns:
    - infra-local blame substrate with typed request/result models
    - trusted SCIP artifacts must carry explicit index freshness metadata
key-files:
  created:
    - src/atelier/infra/code_intel/git_history/blame.py
    - tests/infra/code_intel/git_history/test_blame.py
  modified:
    - src/atelier/infra/code_intel/git_history/models.py
    - src/atelier/infra/code_intel/scip/reader.py
    - src/atelier/infra/code_intel/scip/adapter.py
    - tests/infra/code_intel/scip/test_scip_adapter.py
key-decisions:
  - "Keep stale-index handling infra-local in this wave by emitting freshness metadata on blame requests instead of wiring public errors early."
  - "Require trusted SCIP artifacts to declare a 40-character index_sha and copy it into routed symbol payloads for later HEAD comparisons."
patterns-established:
  - "Git-history blame lives in git_history/blame.py with typed request/result models and local cache reuse."
  - "Trusted SCIP reader validation rejects missing freshness metadata before routed providers can assume HEAD."
requirements-completed: [HIST-02]
duration: 2 min
completed: 2026-05-19
---

# Phase 4 Plan 03: Blame substrate and SCIP freshness Summary

**Typed pygit2 blame/churn annotations plus explicit SCIP index_sha freshness metadata for later stale-index orchestration.**

## Performance

- **Duration:** 2 min
- **Started:** 2026-05-19T12:10:17Z
- **Completed:** 2026-05-19T12:12:38Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments
- Added a dedicated `git_history/blame.py` substrate with typed blame requests, line-span aggregation, churn scoring, cache reuse, and local-edits metadata.
- Extended git-history models with typed freshness-aware blame payloads without wiring engine or MCP surfaces.
- Propagated required `index_sha` freshness metadata through the trusted SCIP reader and routed symbol payloads, with explicit failure coverage for missing or malformed metadata.

## Task Commits

Each task was committed atomically:

1. **Task 1: Build the dedicated blame/churn substrate under `git_history/`**
   - `3a7d36e` test(04-03): add failing tests for blame substrate
   - `9085d40` feat(04-03): add blame and churn substrate
2. **Task 2: Propagate explicit index freshness metadata through the SCIP seam**
   - `70d5851` test(04-03): add failing freshness metadata tests
   - `b9ae1e1` feat(04-03): propagate scip freshness metadata

**Plan metadata:** pending final docs commit

## Files Created/Modified
- `src/atelier/infra/code_intel/git_history/blame.py` - infra-local blame annotator with pygit2 blame aggregation, churn scoring, freshness tagging, and local-edit detection
- `src/atelier/infra/code_intel/git_history/models.py` - typed blame request, hunk, churn, and annotation models
- `src/atelier/infra/code_intel/scip/reader.py` - validates and loads required `index_sha` freshness metadata from trusted SCIP artifacts
- `src/atelier/infra/code_intel/scip/adapter.py` - preserves routed freshness metadata and exposes artifact index SHA access
- `tests/infra/code_intel/git_history/test_blame.py` - real git fixture coverage for blame metadata, cache reuse, stale freshness, and local edits
- `tests/infra/code_intel/scip/test_scip_adapter.py` - regression coverage for explicit `index_sha` propagation and invalid freshness payload rejection

## Decisions Made
- Kept stale-index behavior infra-local for Wave 3 by returning explicit freshness metadata on blame annotations rather than wiring public `index_stale` responses before Wave 4.
- Treated missing or malformed SCIP freshness metadata as trusted-artifact failures, preventing implicit HEAD assumptions at the provider seam.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- Context7 CLI fallback was unavailable in the environment, so pygit2 API details were confirmed from the installed pinned package's runtime docstrings.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Wave 4 can now wire `code op="blame"` against typed blame annotations and compare routed `index_sha` values against HEAD explicitly.
- No MCP surface, benchmark closeout, or validation-matrix work was mixed into this wave.

## Self-Check

PASSED

---
*Phase: 04-historical-code-intelligence*
*Completed: 2026-05-19*
