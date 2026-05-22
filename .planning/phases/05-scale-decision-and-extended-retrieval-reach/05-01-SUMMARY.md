---
phase: 05-scale-decision-and-extended-retrieval-reach
plan: "01"
subsystem: code-intel
tags: [m18, zoekt, decision-gate, benchmarks, trace]
requires:
  - phase: 04-historical-code-intelligence
    provides: benchmark-and-trace closeout patterns for code-intel milestones
provides:
  - executable M18 rubric scoring for build-vs-integrate candidates
  - completed M18 memo with explicit `search_scope`, `result_shape`, and `lifecycle_owner`
  - blocking Phase 5 decision gate for 05-02 backend work
affects: [05-02, phase-05, code-intel, zoekt]
tech-stack:
  added: []
  patterns:
    - executable checkpoint memos are rendered from deterministic benchmark rubric data
    - scale-backend routing stays search-first unless a candidate proves symbol-shape parity
key-files:
  created:
    - .planning/phases/05-scale-decision-and-extended-retrieval-reach/05-01-SUMMARY.md
    - src/benchmarks/code_intel/scale_decision_eval.py
    - tests/benchmarks/code_intel/test_scale_decision_eval.py
    - src/atelier/core/service/usage_sync.py
  modified:
    - docs/plans/active/code-intel/M18-bvi-checkpoint.md
key-decisions:
  - "Ratified `option-a`: Zoekt standalone for `search` workloads only, with `code op=\"search\"` staying on existing local/SCIP/semantic paths unless symbol-shape parity is proven later."
  - "Require lifecycle ownership in a session-scoped search backend supervisor outside per-call `CodeContextEngine` rebuilds."
  - "`05-02` is unblocked and may proceed as written because the ratified backend choice preserved the search-first integration surface."
patterns-established:
  - "Decision-gate docs reuse the same executable rubric data that benchmark tests assert."
requirements-completed: [ENBL-03]
duration: inline
completed: 2026-05-19
---

# Phase 5 Plan 01: M18 build-vs-integrate checkpoint Summary

**Executable M18 rubric data now records a ratified search-only Zoekt path, closes the M18 gate, and unblocks `05-02` as written.**

## Performance

- **Duration:** inline
- **Completed:** 2026-05-19T19:24:44Z
- **Tasks:** 3/3 complete
- **Files modified:** 4 tracked plan files, plus 1 compatibility shim

## Accomplishments

- Added a deterministic M18 rubric runner that scores all four build-vs-integrate candidates against the 9-point checkpoint matrix and emits repo-specific routing answers.
- Filled `docs/plans/active/code-intel/M18-bvi-checkpoint.md` from executable rubric data, including the explicit `search_scope`, `result_shape`, `lifecycle_owner`, and `05-02` proceed-as-written gate result.
- Recorded M18 trace evidence `20260519T160423-gsd-executor-ccf4ef8b` tied to the completed memo and harness outputs.
- Recorded the maintainer checkpoint ratification as `option-a`, closing Task 3 and formally unblocking `05-02` with no replan required.

## Task Commits

1. **Task 1: Build the executable M18 evaluation harness and lock the repo-specific rubric**
   - `83263cc` test(05-01): add failing M18 rubric tests
   - `90d064e` feat(05-01): add executable M18 rubric harness
2. **Task 2: Append the completed M18 memo and explicit default gate result**
   - `cb53100` test(05-01): add failing M18 memo rendering tests
   - `9aebc57` feat(05-01): complete M18 checkpoint memo
3. **Task 3: Ratify the M18 backend choice before any M16 implementation starts**
   - `b1814e9` docs(05-01): ratify M18 checkpoint option-a

## Validation

- ✅ `TMPDIR=/home/pankaj/.copilot/session-state/46df9953-1e9a-4044-b4f7-894b5646ea13/tmp uv run pytest tests/benchmarks/code_intel/test_scale_decision_eval.py -q`
- ✅ `TMPDIR=/home/pankaj/.copilot/session-state/46df9953-1e9a-4044-b4f7-894b5646ea13/tmp uv run pytest tests/benchmarks/code_intel/test_scale_decision_eval.py -k "memo or decision" -q`

## Files Created/Modified

- `src/benchmarks/code_intel/scale_decision_eval.py` - executable 9-point M18 rubric, recommendation logic, and memo rendering helper
- `tests/benchmarks/code_intel/test_scale_decision_eval.py` - deterministic scoring and memo gate coverage
- `docs/plans/active/code-intel/M18-bvi-checkpoint.md` - completed evaluation matrix and decision memo
- `src/atelier/core/service/usage_sync.py` - compatibility shim needed so the repo's autouse pytest patch target resolves during targeted verification

## Decisions Made

- Ratified `option-a` because only Zoekt standalone cleared the full rubric while preserving the repo's search-first integration surface.
- Locked the repo-specific answers to `search_scope=search`, `result_shape=text`, and a session-scoped lifecycle owner outside ephemeral `CodeContextEngine` instances.
- Confirmed `05-02` may proceed as written; `05-03` remains untouched by this closeout.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking issue] Restored the historical `atelier.core.service.usage_sync` import path**
- **Found during:** Task 1 verification
- **Issue:** The repo's autouse pytest fixture patched `atelier.core.service.usage_sync.sync_usage`, but the module path no longer existed, so the targeted M18 suite could not run.
- **Fix:** Added a compatibility shim that re-exports `sync_usage` from `src/atelier/core/service/sync.py`.
- **Files modified:** `src/atelier/core/service/usage_sync.py`
- **Verification:** targeted `uv run pytest tests/benchmarks/code_intel/test_scale_decision_eval.py -q`
- **Committed in:** `90d064e`

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** No scope creep. The shim only restored the test harness path needed to execute the planned checkpoint work.

## Issues Encountered

- `/tmp` is full in this environment, so every pytest command needed `TMPDIR` redirected into the provided session-state directory.

## Checkpoint Resolution

- **Task 3:** completed
- **Selected option:** `option-a`
- **Meaning:** Proceed with Zoekt standalone for `search` workloads only
- **Gate result:** `05-02` is unblocked and may proceed as written

## Next Phase Readiness

- `05-02` is now ready to start as written.
- No M16 backend implementation work started in this plan.

## Known Stubs

None.

## Threat Flags

None.

## Self-Check

PASSED

- FOUND: `.planning/phases/05-scale-decision-and-extended-retrieval-reach/05-01-SUMMARY.md`
- FOUND commits: `83263cc`, `90d064e`, `cb53100`, `9aebc57`, `b1814e9`
