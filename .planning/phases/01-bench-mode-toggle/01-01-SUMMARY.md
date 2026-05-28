---
phase: 01-bench-mode-toggle
plan: "01"
subsystem: bench
tags: [bench-mode, singleton, environment-gate, telemetry, cli-bootstrap]
dependency_graph:
  requires: []
  provides:
    - atelier.bench.mode — BenchMode singleton, bootstrap(), is_off(), mode(), make_arm_env()
    - atelier.bench — public re-export hub
    - environment.mcp_tool_visible_to_llm — bench-first gate
    - cli.main() — bench bootstrap as first line
  affects:
    - src/atelier/core/environment.py
    - src/atelier/gateway/cli/app.py
tech_stack:
  added:
    - StrEnum (Python 3.11 stdlib) — replaces str+Enum inheritance pattern
  patterns:
    - Module-level singleton frozen at bootstrap() call; idempotent on double-call
    - Lazy-bootstrap fallback for test code that skips main()
key_files:
  created:
    - src/atelier/bench/mode.py
    - src/atelier/bench/__init__.py
  modified:
    - src/atelier/core/environment.py
    - src/atelier/gateway/cli/app.py
decisions:
  - Used StrEnum (py311+) instead of BenchMode(str, Enum) to satisfy ruff UP042; functionally identical
  - bench-off check in mcp_tool_visible_to_llm placed before is_dev_mode() per MODE-04 threat model
  - Tasks 3 and 4 committed together as a single app.py change (both target same file)
metrics:
  duration: ~10 minutes
  completed: "2025-01-09"
  tasks_completed: 4
  files_changed: 4
---

# Phase 1 Plan 01: BenchMode Singleton, Environment Gate, CLI Bootstrap Summary

**One-liner:** BenchMode(StrEnum) singleton with bootstrap/is_off/make_arm_env, bench-off short-circuits MCP tool visibility before dev-mode, CLI main() bootstraps mode on first line, session_start telemetry tagged with bench_mode.

## What Was Built

Created the `src/atelier/bench/` package as a stdlib-only leaf module providing the single source of truth for bench-mode state. Wired the mode into the MCP tool visibility gate and the CLI entry point.

### src/atelier/bench/mode.py (new)
- `BenchMode(StrEnum)`: `ON = "on"`, `OFF = "off"`
- `bootstrap()`: reads `ATELIER_BENCH_MODE` env var once, freezes module-level `_mode`; idempotent (no-op on second call)
- `is_off() -> bool`: lazy-bootstraps if `_mode is None`; returns `True` only for `ATELIER_BENCH_MODE=off`
- `mode() -> BenchMode`: lazy-bootstraps; returns current mode
- `make_arm_env(atelier_root, *, mode=None) -> dict[str, str]`: copies `os.environ`, sets `ATELIER_ROOT` and `ATELIER_BENCH_MODE` for subprocess isolation

### src/atelier/bench/__init__.py (new)
- Re-exports all 5 public symbols from `mode.py`
- Zero internal atelier dependencies — stdlib only

### src/atelier/core/environment.py (modified)
- Added `from atelier.bench.mode import is_off as _bench_is_off`
- `mcp_tool_visible_to_llm`: bench-off check runs **before** `is_dev_mode()` — satisfies T-01-01 threat mitigation (MODE-04)

### src/atelier/gateway/cli/app.py (modified)
- Top-level import: `from atelier.bench import bootstrap as _bench_bootstrap`
- `main()`: `_bench_bootstrap()` as first executable line (MODE-05)
- `_begin_cli_telemetry()`: `from atelier.bench.mode import mode as _bench_mode`; `bench_mode=_bench_mode().value` added to `session_start` emit only

## Verification Results

```
1. Package structure: __init__.py  mode.py  ✓
2. ATELIER_BENCH_MODE=off → is_off() True  ✓
3. ATELIER_BENCH_MODE=off ATELIER_DEV_MODE=1 → mcp_tool_visible_to_llm('compact') False  ✓
4. grep -c "_bench_bootstrap()" app.py → 1  ✓
5. ruff check: All checks passed  ✓
   mypy --strict: no issues found in 3 source files  ✓
6. tests/core/test_environment_policy.py: 4 passed  ✓
```

## Commits

| Hash | Description |
|------|-------------|
| `7ded051` | feat(bench): create bench package — BenchMode singleton + make_arm_env |
| `bb37c98` | feat(bench): environment.py — bench-off short-circuits mcp_tool_visible_to_llm |
| `4658941` | feat(bench): cli/app.py — bench bootstrap in main(), bench_mode tag in session_start |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] BenchMode(str, Enum) → StrEnum to satisfy ruff UP042**
- **Found during:** Task 1 — lint check after creating mode.py
- **Issue:** Plan specified `BenchMode(str, Enum)` but project uses `ruff` with `select = ["UP"]` (pyupgrade rules). UP042 requires StrEnum for py311+ targets. ruff `--unsafe-fixes` would have auto-fixed it anyway.
- **Fix:** Changed inheritance to `BenchMode(StrEnum)` with `from enum import StrEnum`. Functionally identical — StrEnum is str + Enum combined for py311+.
- **Files modified:** `src/atelier/bench/mode.py`
- **Commit:** `7ded051`

## Known Stubs

None — all functions have full implementations.

## Threat Flags

None — no new network endpoints, auth paths, file access patterns, or schema changes introduced.

## Self-Check

- [x] `src/atelier/bench/mode.py` exists
- [x] `src/atelier/bench/__init__.py` exists
- [x] `src/atelier/core/environment.py` modified
- [x] `src/atelier/gateway/cli/app.py` modified
- [x] Commits 7ded051, bb37c98, 4658941 exist
