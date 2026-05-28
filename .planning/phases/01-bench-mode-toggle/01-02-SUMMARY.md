---
phase: "01"
plan: "02"
subsystem: bench-mode-toggle
tags: [bench-mode, routing, compression, memory, passthrough-guards]
dependency_graph:
  requires: ["01-01"]
  provides: ["MODE-01", "MODE-02", "MODE-03"]
  affects:
    - src/atelier/core/capabilities/cross_vendor_routing/
    - src/atelier/core/capabilities/model_routing/
    - src/atelier/core/capabilities/context_compression/
    - src/atelier/core/capabilities/cross_vendor_memory/
tech_stack:
  added: []
  patterns:
    - lazy-import bench guard (from atelier.bench.mode import is_off as _bench_is_off)
    - passthrough classmethod on result dataclass (CompressionResult.passthrough())
key_files:
  created: []
  modified:
    - src/atelier/core/capabilities/cross_vendor_routing/router.py
    - src/atelier/core/capabilities/cross_vendor_routing/advisor.py
    - src/atelier/core/capabilities/model_routing/router.py
    - src/atelier/core/capabilities/context_compression/models.py
    - src/atelier/core/capabilities/context_compression/capability.py
    - src/atelier/core/capabilities/cross_vendor_memory/registry.py
decisions:
  - "CrossVendorRouter.recommend() and ModelRouter.score() widened to | None return type for bench-off passthrough; callers updated accordingly"
  - "CompressionResult.passthrough() classmethod used as zero-work sentinel (all zeros, empty lists)"
  - "MemoryRegistry._load() returns [] before cache check — avoids touching adapter layer entirely in bench-off mode"
  - "advisor.py recommend() null-guarded to return {configured: False, bench_off: True} when router returns None"
metrics:
  duration: "~8 minutes"
  completed: "2025-07-09"
  tasks_completed: 3
  files_modified: 6
---

# Phase 1 Plan 2: Capability Passthrough Guards Summary

**One-liner:** Lazy bench-off guards added to cross-vendor router, model router, context compressor, and memory registry — all return zero-cost passthrough values when `ATELIER_BENCH_MODE=off`.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Router passthrough guards (MODE-01) | 724bcf2 | cross_vendor_routing/router.py, model_routing/router.py |
| 1a | Advisor null guard (MODE-01 follow-on) | fa13519 | cross_vendor_routing/advisor.py |
| 2 | Compressor passthrough (MODE-02) | 6891d7a | context_compression/models.py, capability.py |
| 3 | Memory registry passthrough (MODE-03) | da1ed84 | cross_vendor_memory/registry.py |

## Changes Made

### MODE-01: Router Guards

**`cross_vendor_routing/router.py`**
- `CrossVendorRouter.recommend()` return type widened to `CrossVendorRecommendation | None`
- Lazy import + `if _bench_is_off(): return None` as first lines of method body
- `_recommend_for_vendor()` null-guards `router.score()` result (type safety)

**`model_routing/router.py`**
- `ModelRouter.score()` return type widened to `ModelRecommendation | None`
- Lazy import + `if _bench_is_off(): return None` as first lines of method body

### MODE-02: Compressor Guards

**`context_compression/models.py`**
- `CompressionResult.passthrough()` classmethod added — returns zero-field instance
- UP037 fix: removed string quotes from return annotation (ruff-compliant)

**`context_compression/capability.py`**
- `compress_with_provenance()`: lazy import + early return via `CompressionResult.passthrough()`
- `compress_with_sleeptime()`: defense-in-depth guard (prevents both LLM calls and SQLite writes)

### MODE-03: Memory Registry Guard

**`cross_vendor_memory/registry.py`**
- `MemoryRegistry._load()`: lazy import + `return []` before cache check — bypasses all vendor adapters in bench-off mode

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing null guard] `CrossVendorRouteAdvisor.recommend()` in advisor.py**
- **Found during:** Post-Task-1 mypy strict check
- **Issue:** `recommend()` return type widened to `CrossVendorRecommendation | None` caused 11 `union-attr` mypy errors in `advisor.py` which calls the router directly and dereferences the result without a None check
- **Fix:** Added `if recommendation is None: return {"configured": False, "bench_off": True}` after the `router.recommend()` call
- **Files modified:** `src/atelier/core/capabilities/cross_vendor_routing/advisor.py`
- **Commit:** fa13519

## Verification

- `uv run ruff check src/atelier/core/capabilities/` — ✅ All checks passed
- `uv run mypy --strict` on all 4 capability directories — ✅ Success: no issues found in 21 source files
- Smoke test `ATELIER_BENCH_MODE=off` with `MemoryRegistry` — ✅ `all_facts()` returned 0 items as expected

## Self-Check: PASSED

- `src/atelier/core/capabilities/cross_vendor_routing/router.py` — ✅ exists
- `src/atelier/core/capabilities/cross_vendor_routing/advisor.py` — ✅ exists
- `src/atelier/core/capabilities/model_routing/router.py` — ✅ exists
- `src/atelier/core/capabilities/context_compression/models.py` — ✅ exists
- `src/atelier/core/capabilities/context_compression/capability.py` — ✅ exists
- `src/atelier/core/capabilities/cross_vendor_memory/registry.py` — ✅ exists
- Commit 724bcf2 — ✅ exists
- Commit 6891d7a — ✅ exists
- Commit da1ed84 — ✅ exists
- Commit fa13519 — ✅ exists
