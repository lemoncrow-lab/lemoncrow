---
phase: 23-silent-exception-audit
verified: 2026-05-29T18:35:48Z
status: passed
score: 4/4 must-haves verified
overrides_applied: 0
---

# Phase 23: Silent Exception Audit Verification Report

**Phase Goal:** Remove silent `except Exception: pass` blocks or make intentional suppression observable; shrink BLE001 ignores for fixed files.
**Verified:** 2026-05-29T18:35:48Z
**Status:** PASSED
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
| --- | ----- | ------ | -------- |
| 1 | No silent broad-except `pass` sites remain unless explicitly justified inline | ✓ VERIFIED | AST inventory across `src/` returns `broad_except_pass_sites=0`. The 2 raw `grep` matches (`cli/app.py:3648`, `context_compression/capability.py:195`) are false positives — substring "pass"/"passages" inside message strings, bodies are `raise`/`_log.warning`, not `pass`. |
| 2 | Best-effort suppressions log with `exc_info=True`; true failures surface or re-raise | ✓ VERIFIED | All 15 in-scope files carry `getLogger` + `exc_info=True` (counts confirmed per file). Narrowed handlers (`environment.py` → `(tomllib.TOMLDecodeError, OSError, ValueError)`; mcp readers → `(OSError, json.JSONDecodeError)`; benchmark parse loops → realistic tuples) preserve fail-open fallbacks. |
| 3 | BLE001 ignores shrink for fixed files | ✓ VERIFIED | 8 per-file-ignore lines removed (sdk x3, `core/environment.py`, `compact_bench`, `routing_bench`, `routing_quality_bench`, `runner.py`). `uv run ruff check src --select BLE001` → All checks passed. Retained ignores (engine, git_history, letta, routing_replay_bench, report.py, compact_quality_bench) still present by design. |
| 4 | Gateway/MCP focused tests cover touched protocol surfaces | ✓ VERIFIED | `test_p0_mcp_surfaces.py` → 35 passed. No new `print()` in `mcp_server.py` (count stays at 1, pre-existing). `isinstance(data, dict)` guards present at lines 605/661/691/714/820 (fc52a9b malformed-JSON fail-open fix). |

**Score:** 4/4 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
| -------- | -------- | ------ | ------- |
| `mcp_server.py` | 9 in-scope handlers observable; no new print | ✓ VERIFIED | 15 `exc_info=True`, print count unchanged (1), isinstance guards added |
| `cli/app.py` | 4 in-scope handlers observable | ✓ VERIFIED | 8 `exc_info=True`, 1 narrowed (`_detect_git_root`), 3 best-effort logged |
| `sdk/anthropic_tools.py`, `gemini_adk.py`, `langchain_middleware.py` | loggers + logged callbacks; BLE001 ignores removed | ✓ VERIFIED | loggers added, `exc_info=True` present, 3 ignore lines absent |
| `core/environment.py` | narrowed to `(TOMLDecodeError, OSError, ValueError)`; ignore removed | ✓ VERIFIED | narrowing + `logger.warning(...exc_info=True)` at L164-166; ignore absent |
| `engine.py`, `git_history/adapter.py`, `letta_adapter.py` | observable; ignores retained | ✓ VERIFIED | loggers + `exc_info=True`; ignores still present |
| benchmark files (6) + `pyproject.toml` | parse loops narrowed; 4 ignores removed | ✓ VERIFIED | loggers + realistic tuples; 4 benchmark ignore lines absent; routing_replay_bench + report.py retain ignores |
| `tests/infra/test_memory_backend_selection.py` | invalid-config caplog regression | ✓ VERIFIED | `test_invalid_config_toml_falls_back_and_warns` present with `caplog` assertion |

### Key Link Verification

| From | To | Via | Status | Details |
| ---- | -- | --- | ------ | ------- |
| modified source files | stderr logging | `logger/_log.debug/.warning(..., exc_info=True)` | ✓ WIRED | All 15 files emit through module loggers, no `print()` introduced |
| `pyproject.toml` | ruff BLE001 | removed 8 per-file-ignore lines | ✓ WIRED | `ruff check src --select BLE001` green after deletions |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| -------- | ------- | ------ | ------ |
| Tree-wide silent pass = 0 | AST walk of `src/**/*.py` | `broad_except_pass_sites=0` | ✓ PASS |
| BLE001 lint green | `uv run ruff check src --select BLE001` | All checks passed | ✓ PASS |
| Full ruff green | `uv run ruff check src` | All checks passed | ✓ PASS |
| Memory backend + SDK middleware tests | `pytest tests/infra/test_memory_backend_selection.py tests/gateway/test_sdk_middleware.py` | 34 passed | ✓ PASS |
| P0 MCP surfaces | `pytest tests/gateway/test_p0_mcp_surfaces.py` | 35 passed | ✓ PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| ----------- | ----------- | ----------- | ------ | -------- |
| QBL-EXC-01 | 23-01 | Fresh enumeration captured before fixes | ✓ SATISFIED | 23-RESEARCH baseline + before/after table in 23-01-SUMMARY (28 → 0) |
| QBL-EXC-02 | 23-01/02/03 | Every silent broad-except removed/narrowed/logged with rationale | ✓ SATISFIED | AST inventory = 0; all handlers narrowed or logged with `exc_info=True` |
| QBL-EXC-03 | 23-02/03 | Fixed files removed from BLE001 ignores | ✓ SATISFIED | 8 ignore lines removed; ruff BLE001 green; retained ignores documented |
| QBL-EXC-04 | 23-01 | MCP/tool-handler focused tests cover touched gateway surfaces | ✓ SATISFIED | P0 MCP 35 passed; no new print; isinstance fail-open guards |

### Implementation Commits

| Commit | Description | Status |
| ------ | ----------- | ------ |
| `0df47e4` | observe gateway silent exceptions | ✓ present |
| `6f08d89` | observe sdk middleware silent exceptions | ✓ present |
| `c86ca8d` | observe core/infra silent exceptions | ✓ present |
| `f466ca9` | observe benchmark silent exceptions | ✓ present |
| `fc52a9b` | preserve malformed json fail-open paths | ✓ present |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| ---- | ---- | ------- | -------- | ------ |
| `mcp_server.py` | 5439 | `import subprocess` inside guarded `try` (IN-01, from 23-REVIEW) | ℹ️ Info | Theoretical only — stdlib always importable; non-blocking, optional cleanup |

No blockers or warnings found. No unreferenced debt markers (TBD/FIXME/XXX) introduced by this phase.

### Human Verification Required

None — all success criteria are programmatically verifiable (lint, AST inventory, focused tests).

### Gaps Summary

No gaps. All four ROADMAP success criteria and all four QBL-EXC requirements are satisfied in the codebase, independently confirmed by AST inventory (0 silent broad-except-pass sites), green `ruff --select BLE001`, removal of the 8 fully-clean per-file ignores, and passing focused MCP/SDK/memory tests.

**Unrelated baseline failures (recorded, not Phase 23 deliverables — per VALIDATION "Known Baseline"):**
- `make typecheck` fails on pre-existing dirty WIP at `mcp_server.py:1074` (redundant cast) — outside the 13 Phase-23 hunks.
- `make format-check` fails on unrelated dirty files (autopilot capability/test, SCIP registry test, context_reuse capability) — not touched by this phase.
- 4 order-dependent flaky failures in `test_mcp_tool_handlers.py` from module-singleton state leakage — reproduce on a clean `HEAD` worktree without this diff; documented test-isolation defect, not a silent-exception site.

These are unrelated baseline issues that the phase correctly did not attempt to fix, consistent with the worktree-preservation constraint. They do not affect Phase 23 goal achievement.

---

_Verified: 2026-05-29T18:35:48Z_
_Verifier: the agent (gsd-verifier)_
