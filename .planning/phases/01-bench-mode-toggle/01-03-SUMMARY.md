---
phase: "01"
plan: "03"
subsystem: bench-mode-toggle
tags: [testing, bench-mode, unit-tests, integration-tests]
dependency_graph:
  requires:
    - 01-01-PLAN (BenchMode singleton + bench/mode.py)
    - 01-02-PLAN (capability guards in router, advisor, compression, memory)
  provides:
    - Verified regression safety for MODE-01 through MODE-08
  affects:
    - tests/core/test_bench_mode.py
    - tests/core/test_bench_mode_integration.py
tech_stack:
  added: []
  patterns:
    - sys.modules workaround for atelier.bench.__init__ function/submodule name collision
    - autouse monkeypatch fixture for singleton reset between tests
key_files:
  created:
    - tests/core/test_bench_mode.py
    - tests/core/test_bench_mode_integration.py
  modified: []
decisions:
  - "Use sys.modules['atelier.bench.mode'] to access the mode submodule directly, avoiding the name collision where atelier/bench/__init__.py re-exports a function named `mode` that shadows the submodule at attribute lookup"
  - "autouse fixture clears both _mode singleton and ATELIER_BENCH_MODE + ATELIER_DEV_MODE env vars to ensure test isolation"
metrics:
  duration: "~10 minutes"
  completed: "2025-07-17"
---

# Phase 1 Plan 03: Bench Mode Tests Summary

Unit tests and integration test for the BenchMode singleton and all capability guards introduced in Waves 1 and 2.

## What Was Built

### Task 1 — Unit tests (`tests/core/test_bench_mode.py`)

13 unit tests covering MODE-01 through MODE-06:

| Test | Requirement | Result |
|------|-------------|--------|
| `test_is_off_when_env_off` | MODE-01 | ✅ PASS |
| `test_is_on_when_env_on` | MODE-01 | ✅ PASS |
| `test_is_on_when_env_absent` | MODE-02 (default=on) | ✅ PASS |
| `test_is_off_case_insensitive` | MODE-01 | ✅ PASS |
| `test_bootstrap_is_idempotent` | MODE-03 | ✅ PASS |
| `test_mode_returns_bench_mode` | MODE-01 | ✅ PASS |
| `test_make_arm_env_sets_root` | MODE-05 | ✅ PASS |
| `test_make_arm_env_preserves_env` | MODE-05 | ✅ PASS |
| `test_mcp_tools_hidden_bench_off` | MODE-04 | ✅ PASS |
| `test_mcp_tools_visible_bench_on` | MODE-04 | ✅ PASS |
| `test_bench_off_overrides_dev_mode` | MODE-04 | ✅ PASS |
| `test_memory_returns_empty_bench_off` | MODE-06 | ✅ PASS |
| `test_compression_passthrough` | MODE-06 | ✅ PASS |

**Total: 13/13 passed**

### Task 2 — Integration test (`tests/core/test_bench_mode_integration.py`)

1 integration test (`@pytest.mark.slow`) covering MODE-08:

| Test | Requirement | Result |
|------|-------------|--------|
| `test_bench_on_vs_off_mcp_tool_counts_differ` | MODE-08 | ✅ PASS |

`on_count` = 17 (all STABLE_LLM_TOOLS visible), `off_count` = 0.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Name collision between `atelier.bench.mode` submodule and `mode` function**
- **Found during:** Task 1 (first test run attempt)
- **Issue:** `atelier/bench/__init__.py` exports `from atelier.bench.mode import mode`, setting `atelier.bench.mode` as an attribute of the package to the *function* `mode`. Doing `import atelier.bench.mode as _bm` binds `_bm` to the function (not the module) when the `__init__.py` attribute shadows the submodule. `monkeypatch.setattr(_bm, "_mode", None)` then failed with `AttributeError`.
- **Fix:** Use `sys.modules["atelier.bench.mode"]` to retrieve the actual module object, bypassing the attribute-lookup collision. Added `import atelier.bench.mode  # noqa: F401` before the `sys.modules` access to ensure the module is loaded.
- **Files modified:** `tests/core/test_bench_mode.py`, `tests/core/test_bench_mode_integration.py`
- **No separate commit** — fixed inline before any commit

## Regression Check

Ran `uv run pytest tests/core/test_bench_mode.py tests/core/test_environment_policy.py tests/core/capabilities/ -q -m "not slow"` — **166 passed, 0 failures**.

## Known Stubs

None — test files only; no UI or data-flow stubs.

## Threat Flags

None — test files introduce no new network endpoints, auth paths, or schema changes.

## Self-Check: PASSED

- `tests/core/test_bench_mode.py` — FOUND ✅
- `tests/core/test_bench_mode_integration.py` — FOUND ✅
- Commit `30daabb` (unit tests) — FOUND ✅
- Commit `5075007` (integration test) — FOUND ✅
