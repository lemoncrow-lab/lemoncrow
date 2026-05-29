---
phase: 23-silent-exception-audit
plan: 02
subsystem: sdk-core-infra-observability
tags: [silent-exceptions, logging, ruff-ble001, observability]
requires:
  - 23-01 (gateway silent exceptions observable)
provides:
  - "9 SDK/core/infra silent handlers made observable with exc_info=True"
  - "environment.py invalid-config handler narrowed to (TOMLDecodeError, OSError, ValueError)"
  - "4 BLE001 per-file-ignore lines removed (anthropic_tools, gemini_adk, langchain_middleware, environment)"
affects:
  - src/atelier/sdk
  - src/atelier/core/environment.py
  - src/atelier/core/capabilities/code_context/engine.py
  - src/atelier/infra/code_intel/git_history/adapter.py
  - src/atelier/infra/memory_bridges/letta_adapter.py
tech-stack:
  added: []
  patterns:
    - "module logger via logging.getLogger(__name__) (cline.py convention)"
    - "best-effort handlers keep broad catch + noqa: BLE001 + logger.debug(exc_info=True)"
    - "removable handlers narrowed to realistic exception types and ignore deleted"
key-files:
  created: []
  modified:
    - src/atelier/sdk/anthropic_tools.py
    - src/atelier/sdk/gemini_adk.py
    - src/atelier/sdk/langchain_middleware.py
    - src/atelier/core/environment.py
    - src/atelier/core/capabilities/code_context/engine.py
    - src/atelier/infra/code_intel/git_history/adapter.py
    - src/atelier/infra/memory_bridges/letta_adapter.py
    - tests/infra/test_memory_backend_selection.py
    - pyproject.toml
decisions:
  - "letta_client not installed in this env; per Assumption A3 kept broad catch + logger.debug for upsert_block/get_block fallbacks"
  - "SDK prefix-cache/token capture handlers retained broad catch with noqa (must not break host model call) rather than narrowing"
metrics:
  duration: ~25m
  completed: 2026-05-29
requirements: [QBL-EXC-02, QBL-EXC-03]
---

# Phase 23 Plan 02: SDK/Core/Infra Silent Exception Audit Summary

Made the 9 in-scope silent broad-except handlers in the SDK middleware, core, and infra tiers
observable (module loggers + `exc_info=True`), narrowed `environment.py`'s invalid-config handler
to realistic exception types, and removed the 4 now-fully-clean files from the `BLE001` ignore
ledger while keeping ruff green.

## What Was Built

### Task 1 — SDK middleware callbacks observable (commit 6f08d89)
- Added module loggers (`logging.getLogger(__name__)`) to `anthropic_tools.py`, `gemini_adk.py`,
  `langchain_middleware.py` — none had a logger before.
- Replaced 4 silent `except Exception: pass` sites (best-effort cache/token + prefix-cache
  capture) with `logger.debug(..., exc_info=True)`.
- Retained broad catch with inline `# noqa: BLE001 — best-effort ... must not break host model
  call` because these callbacks may raise arbitrary host SDK errors and must never propagate into
  the host model call (RESEARCH Pitfall 3, Assumption A2, threat T-23-04 accept).
- Removed 3 BLE001 per-file-ignore lines for the SDK files.

### Task 2 — core/infra sites observable + environment.py narrowed (commit c86ca8d)
- Added module loggers to `environment.py`, `code_context/engine.py`, `git_history/adapter.py`,
  `letta_adapter.py`.
- `environment.py` `resolve_memory_backend`: narrowed `except Exception` to
  `(tomllib.TOMLDecodeError, OSError, ValueError)`, kept the defaults fallback, replaced `pass`
  with `logger.warning("Invalid config.toml; falling back to defaults", exc_info=True)`
  (threat T-23-05 mitigate).
- `engine.py` `_lineage_bootstrap_worker`: `logger.debug("lineage bootstrap failed", exc_info=True)`
  (fail-open kept).
- `git_history/adapter.py` `_resolved_rename_target`: `logger.debug("rename-target resolution
  failed", exc_info=True)` (heuristic still caches None).
- `letta_adapter.py` `upsert_block` (~93) and `get_block` (~121): kept broad catch (control-flow
  fallbacks) + `logger.debug(..., exc_info=True)`. `letta_client` is not installed in this
  environment, so per Assumption A3 no SDK not-found type was available to narrow to.
- Added caplog regression test `test_invalid_config_toml_falls_back_and_warns` asserting fallback
  to sqlite + a WARNING with exception info.
- Removed the `environment.py` BLE001 per-file-ignore line.

## BLE001 Ignore Ledger Changes (QBL-EXC-03)

**Removed (4 lines, now fully clean):**
- `"src/atelier/sdk/anthropic_tools.py" = ["BLE001"]`
- `"src/atelier/sdk/gemini_adk.py" = ["BLE001"]`
- `"src/atelier/sdk/langchain_middleware.py" = ["BLE001"]`
- `"src/atelier/core/environment.py" = ["BLE001"]`

**Retained (other non-silent broad handlers remain — documented, not a failure):**
- `"src/atelier/core/capabilities/code_context/engine.py" = ["BLE001"]` (8 other broad handlers)
- `"src/atelier/infra/code_intel/git_history/adapter.py" = ["BLE001"]`
- `"src/atelier/infra/memory_bridges/letta_adapter.py" = ["BLE001"]` (17 other broad handlers)

## Validations Run

- `uv run ruff check src --select BLE001` → **All checks passed!** (exit 0)
- `uv run pytest tests/gateway/test_sdk_middleware.py tests/infra/test_letta_adapter_fallback.py tests/infra/test_memory_backend_selection.py -q` → **37 passed**
- Acceptance grep checks:
  - SDK in-scope silent `pass` count: 0
  - core/infra in-scope silent `pass` count: 0
  - `getLogger(__name__)` present in all 7 source files
  - removed ignores (sdk x3 + environment): 0 matches in pyproject.toml
  - retained ignores (engine/git_history/letta): 3 matches
  - environment.py contains `TOMLDecodeError` + `exc_info=True`

## Deviations from Plan

None affecting scope. Notable execution detail:
- Black auto-reformatted the long `logger.debug(...)` comment line in `engine.py` during
  pre-commit; re-staged and committed (no behavior change).
- `letta_client` is not installed, so the optional narrowing of letta fallbacks to an SDK
  not-found type was not possible; kept broad + logged per the plan's Assumption A3 contingency.

## Pre-existing User Work Preserved

`src/atelier/core/environment.py` had an unrelated pre-existing WIP hunk (adding `usages` and
`pattern` to `STABLE_LLM_TOOLS`). It was temporarily reverted to stage only Phase 23 hunks, then
restored — it remains unstaged in the working tree and was NOT committed. No other touched file
had pre-existing changes.

## Known Stubs

None.

## Threat Flags

None — no new security surface introduced; changes only add logging and narrow one handler.

## Self-Check: PASSED

- Commit 6f08d89 (Task 1 SDK) — FOUND
- Commit c86ca8d (Task 2 core/infra) — FOUND
- 23-02-SUMMARY.md — FOUND
- All 7 modified source files verified present with loggers
