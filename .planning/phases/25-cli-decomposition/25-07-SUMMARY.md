---
phase: 25
plan: 07
subsystem: cli
tags: [cli, decomposition, click, thinness]
requires: [25-06]
provides: [cli-thinness-guard, final-app-thinning]
affects:
  - src/atelier/gateway/cli/app.py
  - src/atelier/gateway/cli/commands/lessons.py
  - src/atelier/gateway/cli/commands/sessions.py
  - tests/gateway/test_cli_thinness.py
  - tests/gateway/test_uninstall_cli.py
decisions:
  - Locked app.py thinness with a static regression test instead of moving more registration-adjacent helpers.
  - Kept app.py as a registration/orchestration entrypoint and treated the missing benchmark help-tree surface as a pre-existing dirty-baseline failure.
metrics:
  completed_at: 2026-06-01
---

# Phase 25 Plan 07: Final CLI thinning summary

Locked the final thin app.py shape with a static thinness regression test and fixed Phase 25 test surfaces that still patched pre-extraction app.py internals.

## Outcome

- `src/atelier/gateway/cli/app.py` remains registration/orchestration only, with no direct `subprocess` or `sqlite3` usage.
- Actual `app.py` size at completion: **287 LOC**.
- Added `tests/gateway/test_cli_thinness.py` to enforce the no-`subprocess`/no-`sqlite3` rule, a `<350 LOC` budget, and the public `from atelier.gateway.cli import cli, main` import surface.
- Updated uninstall tests to patch the extracted admin-module `_project_root` symbol so focused validation no longer runs the real uninstall path.

## Validation

### Focused passes

- `grep -vE '^\s*#' src/atelier/gateway/cli/app.py | grep -cE '\b(subprocess|sqlite3)\b'` → `0`
- `wc -l src/atelier/gateway/cli/app.py` → `287`
- `uv run pytest tests/gateway/test_cli_thinness.py -q` → `2 passed`
- `uv run pytest tests/gateway/test_uninstall_cli.py tests/gateway/test_cli_import_progress.py tests/gateway/test_cli_thinness.py -q` → `9 passed`
- `uv run atelier --help`
- `uv run atelier stack run --help`
- `uv run atelier servicectl run --help`
- `uv run atelier systemd --help`
- `uv run ruff check src/atelier/gateway/cli/app.py src/atelier/gateway/cli/commands/lessons.py src/atelier/gateway/cli/commands/sessions.py` → passed

### Full-gate results recorded

- `uv run pytest tests/gateway/test_cli_help_tree.py tests/gateway/test_cli_mcp_only.py tests/gateway/test_cli_thinness.py tests/gateway/test_cli*.py -q` → **fails on pre-existing baseline issues**
  - help tree is **not** byte-identical to the 25-01 baseline (`42a2fedbfbe7864cc64dca686de442bc1f7d9e8a379328af5dec72574179eb0d`); current hash is `94cb0a947978e375a2b92a7761776c34e93a0b99dd2dee82606c675c848abee6`
  - current tree is missing `atelier benchmark`, matching the already-known dirty-baseline benchmark surface drift
  - many broader CLI tests still fail on the unrelated tree-sitter parser thread panic (`_native::Parser is unsendable, but sent to another thread`) triggered by `init`/indexing
- `uv run ruff check src` → **fails on unrelated existing issues** in `integrations/claude/plugin/hooks/stop.py`, `scripts/debug_code_matrix.py`, `scripts/hooks/agent_optimization_hook.py`, and `scripts/mass_replace.py`
- `make lint` → same unrelated Ruff failures
- `make typecheck` → unrelated duplicate-module failure: `benchmarks/__init__.py` vs `src/benchmarks/__init__.py`
- `make test` → unrelated existing parser-thread panic errors across MCP/edit/CLI suites

## Deviations from Plan

### Auto-fixed Issues

1. [Rule 3 - Blocking issue] Focused uninstall validation still patched the old `atelier.gateway.cli.app._project_root` location
   - **Issue:** after extraction, the real callback reads `_project_root` from `atelier.gateway.cli.commands.admin`, so the old patch target no longer intercepted the uninstall flow.
   - **Fix:** updated `tests/gateway/test_uninstall_cli.py` to patch the extracted admin-module symbol directly.
   - **Files:** `tests/gateway/test_uninstall_cli.py`

2. [Rule 3 - Blocking issue] Ruff surfaced small Phase 25 residue in CLI files during final validation
   - **Fix:** cleaned the touched CLI files by removing a stale `__all__` entry from `lessons.py`, normalizing a local import gap in `sessions.py`, and applying Ruff's app.py ordering adjustments.
   - **Files:** `src/atelier/gateway/cli/app.py`, `src/atelier/gateway/cli/commands/lessons.py`, `src/atelier/gateway/cli/commands/sessions.py`

## Known Stubs

None.

## Threat Flags

None.

## Self-Check: PASSED

- Verified summary file exists.
- Verified `tests/gateway/test_cli_thinness.py` exists.
