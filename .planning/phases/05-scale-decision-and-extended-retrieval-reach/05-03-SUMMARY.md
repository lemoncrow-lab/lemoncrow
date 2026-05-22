---
phase: 05-scale-decision-and-extended-retrieval-reach
plan: "03"
subsystem: code-intel
tags: [m17, cross-lang, ctypes, cffi, subprocess, dynamic-import, benchmarks, trace]
requires:
  - phase: 04-historical-code-intelligence
    provides: cache-aware code-intel packing, benchmark smoke patterns, and trace closeout conventions
provides:
  - literal-only static cross-language edge storage under `src/atelier/infra/code_intel/cross_lang/`
  - additive `cross_lang_refs`, `edge_kind`, and `confidence` metadata on `code op="symbol"` and `code op="usages"`
  - benchmark-backed M17 trace evidence for one resolved and one unresolved literal edge
affects: [phase-05, code-intel, m17, cross-lang]
tech-stack:
  added: []
  patterns:
    - cross-language resolver logic lives under a dedicated infra package and leaves `engine.py` to hydration only
    - confidence-tagged cross-language metadata stays additive on existing symbol and usages payloads
key-files:
  created:
    - .planning/phases/05-scale-decision-and-extended-retrieval-reach/05-03-SUMMARY.md
    - src/atelier/infra/code_intel/cross_lang/__init__.py
    - src/atelier/infra/code_intel/cross_lang/AGENT_README.md
    - src/atelier/infra/code_intel/cross_lang/resolvers/ctypes_resolver.py
    - src/atelier/infra/code_intel/cross_lang/resolvers/dynamic_import.py
    - src/atelier/infra/code_intel/cross_lang/resolvers/subprocess_resolver.py
    - src/benchmarks/code_intel/cross_lang_bench.py
    - tests/infra/code_intel/cross_lang/test_edges.py
    - tests/infra/code_intel/cross_lang/test_resolvers.py
    - tests/benchmarks/code_intel/test_cross_lang_bench.py
  modified:
    - src/atelier/infra/code_intel/cross_lang/edges.py
    - src/atelier/infra/code_intel/cross_lang/runner.py
    - src/atelier/core/capabilities/code_context/models.py
    - src/atelier/core/capabilities/code_context/engine.py
    - tests/core/test_code_context.py
key-decisions:
  - "Keep Phase 5 cross-language logic in `src/atelier/infra/code_intel/cross_lang/` and limit `engine.py` changes to typed hydration plus optional-key preservation."
  - "Surface literal-only cross-language metadata additively through `cross_lang_refs`, `edge_kind`, and `confidence` instead of replacing existing local or routed references."
  - "Close M17 with a fixture benchmark that runs the shipped `code` paths, proves one resolved subprocess edge and one unresolved low-confidence cffi edge, and records explicit trace evidence."
patterns-established:
  - "Literal-only resolver orchestration is a dedicated runner contract with an explicit Phase 5 scope ceiling."
  - "Cross-language payload extensions use typed models so budget packing can preserve or drop the new optional keys safely."
requirements-completed: [SCAL-02]
duration: inline
completed: 2026-05-19
---

# Phase 5 Plan 03: Partial cross-language edge resolution with confidence scoring Summary

**Literal-only ctypes/cffi, subprocess, and dynamic-import edges now live in a dedicated infra package and surface additively on `code op="symbol"` and `code op="usages"` with confidence tags.**

## Performance

- **Duration:** inline
- **Completed:** 2026-05-19T19:39:03Z
- **Tasks:** 3/3 complete
- **Files modified:** 14

## Accomplishments

- Added a dedicated `cross_lang` infra seam with typed SQLite-backed edge storage, an explicit Phase 5 scope ceiling, and resolver orchestration for literal-only static patterns.
- Implemented Phase 5 resolvers for literal `ctypes`, `cffi`, `importlib.import_module("...")`, and literal subprocess calls to `.py` entrypoints without touching `mcp_server.py` or widening Phase 6 scope.
- Extended `code op="symbol"` and `code op="usages"` additively so cross-language references retain `edge_kind`, `confidence`, and `provenance="cross_lang"` while preserving existing local results.
- Added benchmark smoke plus trace evidence `20260519T193903-gsd-executor-6cfae359` tied to `docs/plans/active/code-intel/M17-cross-lang.md`.

## Task Commits

1. **Task 1: Create the cross-language edge store and runner contracts**
   - `6498440` test(05-03): add failing cross-language edge store tests
   - `7258e83` feat(05-03): add literal-only cross-language edge storage
2. **Task 2: Implement literal-only resolvers and hydrate `symbol` plus `usages` additively**
   - `8bd8f75` test(05-03): add failing resolver and hydration tests
   - `9fcf463` feat(05-03): add literal-only cross-language hydration
3. **Task 3: Add the cross-language benchmark smoke and budget-aware verification**
   - `7065bd5` feat(05-03): add cross-language benchmark smoke

## Validation

- ✅ `TMPDIR=/home/pankaj/.copilot/session-state/46df9953-1e9a-4044-b4f7-894b5646ea13/tmp uv run pytest tests/infra/code_intel/cross_lang/test_edges.py tests/infra/code_intel/cross_lang/test_resolvers.py tests/core/test_code_context.py tests/benchmarks/code_intel/test_cross_lang_bench.py -k "cross_lang or ctypes or cffi or import_module or subprocess" -q`
- ✅ `TMPDIR=/home/pankaj/.copilot/session-state/46df9953-1e9a-4044-b4f7-894b5646ea13/tmp uv run pytest tests/benchmarks/code_intel/test_cross_lang_bench.py -q`
- ✅ `TMPDIR=/home/pankaj/.copilot/session-state/46df9953-1e9a-4044-b4f7-894b5646ea13/tmp uv run python - <<'PY' ... run_cross_lang_bench(...) ... PY` → trace `20260519T193903-gsd-executor-6cfae359`

## Files Created/Modified

- `src/atelier/infra/code_intel/cross_lang/edges.py` - typed edge model and SQLite store for literal-only cross-language records
- `src/atelier/infra/code_intel/cross_lang/runner.py` - resolver orchestration, symbol resolution, and artifact-signature syncing
- `src/atelier/infra/code_intel/cross_lang/resolvers/ctypes_resolver.py` - literal `ctypes` and `cffi` extraction with confidence tagging
- `src/atelier/infra/code_intel/cross_lang/resolvers/dynamic_import.py` - literal `importlib.import_module("...")` extraction
- `src/atelier/infra/code_intel/cross_lang/resolvers/subprocess_resolver.py` - literal subprocess-to-`.py` entrypoint extraction
- `src/atelier/core/capabilities/code_context/models.py` - typed additive cross-language response fields
- `src/atelier/core/capabilities/code_context/engine.py` - symbol/usages hydration hooks and optional-key preservation for cross-language metadata
- `src/benchmarks/code_intel/cross_lang_bench.py` - fixture-driven M17 smoke benchmark and trace recorder
- `tests/infra/code_intel/cross_lang/test_edges.py` - store and runner contract coverage
- `tests/infra/code_intel/cross_lang/test_resolvers.py` - literal resolver coverage for resolved and unresolved edges
- `tests/core/test_code_context.py` - additive symbol/usages payload coverage
- `tests/benchmarks/code_intel/test_cross_lang_bench.py` - benchmark and trace assertions

## Decisions Made

- Kept all heavy resolver logic in `src/atelier/infra/code_intel/cross_lang/` so `engine.py` stayed limited to typed hydration and optional-key preservation.
- Left `mcp_server.py` untouched and used the existing `tool_code` path for public proof, which kept the Phase 5 implementation additive instead of introducing a new tool surface.
- Limited the shipped resolver set to literal-only static cases and explicitly documented the Phase 5 ceiling against `scope="external"` or multi-repo expansion.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- `/tmp` is full in this environment, so every pytest and benchmark command needed `TMPDIR` redirected into the provided session-state directory.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- `SCAL-02` is complete with benchmark-backed trace evidence, and the cross-language seams are ready for future reuse without bleeding into Phase 6.
- `05-02` remains the only pending Phase 5 implementation plan.

## Known Stubs

None.

## Threat Flags

None.

## Self-Check

PASSED

- FOUND: `.planning/phases/05-scale-decision-and-extended-retrieval-reach/05-03-SUMMARY.md`
- FOUND commits: `6498440`, `7258e83`, `8bd8f75`, `9fcf463`, `7065bd5`
