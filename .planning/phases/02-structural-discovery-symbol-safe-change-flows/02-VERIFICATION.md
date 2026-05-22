---
phase: 02-structural-discovery-symbol-safe-change-flows
verified: 2026-05-19T07:06:49Z
status: human_needed
score: 3/3 requirements verified
reverification: true
overrides_applied: 0
human_verification:
  - test: "Confirm ast-grep bootstrap and binary discovery on the real developer machine"
    expected: "The chosen ast-grep discovery/install path is realistic locally and does not depend on the wrong Linux `sg` binary."
    why_human: "The current environment does not have a real ast-grep install path to validate end to end."
  - test: "Review shared-surface diffs in engine.py, mcp_server.py, and rich_edit.py"
    expected: "Phase 2 keeps the brownfield landing zones narrow and additive while extending the existing MCP/runtime surfaces."
    why_human: "Automated tests prove behavior, but not whether the shared brownfield surfaces still feel maintainable."
  - test: "Exercise a real pattern -> symbol edit -> usages workflow"
    expected: "An operator can find code structurally, edit a named symbol, and inspect usages without reverting to line-number or grep-first workflows."
    why_human: "Benchmarks and regressions validate payloads and behavior, but not the practical operator workflow."
---

# Phase 2: Structural Discovery & Symbol-Safe Change Flows Verification Report

**Phase Goal:** Agents can find code by structure, inspect symbol usages, and apply named-symbol edits without line-number workflows.
**Verified:** 2026-05-19T07:06:49Z
**Status:** human_needed
**Re-verification:** Yes — rerun after the cache-hit-rate telemetry fix for M12 closeout.

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
| --- | --- | --- | --- |
| 1 | `code op="pattern"` ships on the existing `code` surface with explicit ast-grep binary handling, structured match/rewrite behavior, and no regex-silent fallback. | ✓ VERIFIED | `src/atelier/gateway/adapters/mcp_server.py:1928-2074`; `src/atelier/core/capabilities/code_context/engine.py:591-643`; `src/atelier/infra/code_intel/astgrep/binaries.py:193-245`; `src/atelier/infra/code_intel/astgrep/adapter.py:133-223`; `tests/gateway/test_mcp_tool_handlers.py:650-695,737-822`; `tests/infra/code_intel/astgrep/test_astgrep_adapter.py:13-104`. |
| 2 | `edit kind="symbol"` resolves named symbols safely, rejects ambiguous or stale targets clearly, and preserves the existing atomic rich-edit / reindex / memory flow. | ✓ VERIFIED | `src/atelier/core/capabilities/tool_supervision/symbol_edit.py:45-186`; `src/atelier/core/capabilities/tool_supervision/rich_edit.py:257-312`; `src/atelier/gateway/adapters/mcp_server.py:1546-1573`; `tests/core/capabilities/tool_supervision/test_rich_edit_symbol.py:20-96`; `tests/gateway/test_mcp_jsonrpc_e2e.py:497-528`. |
| 3 | `code op="usages"` returns grouped symbol references on the existing `code` surface with routed reference support, explicit treesitter fallback, and M12-compliant cache/budget telemetry. | ✓ VERIFIED | `src/atelier/gateway/adapters/mcp_server.py:2045-2064`; `src/atelier/core/capabilities/code_context/engine.py:534-589,973-1043,1573-1708`; `src/atelier/core/capabilities/code_context/intel_store.py:147-174`; `src/atelier/infra/code_intel/scip/reader.py:73-98,123-172`; `src/atelier/infra/code_intel/scip/adapter.py:104-121`; `src/atelier/core/runtime/engine.py:735-749`; `src/atelier/core/service/telemetry/local_store.py:168-176`; `tests/infra/code_intel/scip/test_scip_adapter.py:203-227`; `tests/gateway/test_mcp_tool_handlers.py:609-633`; `tests/core/test_product_telemetry.py:285-312`. |

**Score:** 3/3 requirement truths verified

## Requirements Coverage

| Requirement | Status | Evidence |
| --- | --- | --- |
| `DISC-01` | ✓ SATISFIED | Symbol-edit seam, gateway wiring, ambiguity/stale-target guards, reindex, and memory tagging verified in `symbol_edit.py`, `rich_edit.py`, `mcp_server.py`, and the symbol-edit core/MCP tests. |
| `DISC-02` | ✓ SATISFIED | Existing `code op="pattern"` surface, ast-grep discovery/bootstrap handling, structured rewrite path, and gateway/infra regressions verified across the ast-grep adapter and MCP handler tests. |
| `NAVG-02` | ✓ SATISFIED | Routed usages lookup, SCIP reference loading, explicit treesitter fallback, grouped payload defaults, and MCP regressions verified across `engine.py`, `intel_store.py`, SCIP adapter/reader, and gateway tests. |

## M12 Closeout

| Layer | Status | Evidence |
| --- | --- | --- |
| Code / telemetry contract | ✓ VERIFIED | Cache/budget freeze remains in `src/atelier/core/capabilities/code_context/budget.py` and cache wiring; cache hit-rate now emits from `src/atelier/core/runtime/engine.py:735-749`, aggregates in `src/atelier/core/service/telemetry/local_store.py:168-176`, and surfaces in `frontend/src/pages/Overview.tsx`. |
| Validation contract | ⚠️ HUMAN NEEDED | `.planning/phases/02-structural-discovery-symbol-safe-change-flows/02-VALIDATION.md` now reflects automated completion, but the manual-only checks and final approval remain intentionally open until a human signs off. |

## Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| --- | --- | --- | --- |
| Phase 2 targeted backend + benchmark suite | `TMPDIR=/home/pankaj/.copilot/session-state/46df9953-1e9a-4044-b4f7-894b5646ea13/tmp uv run pytest tests/core/test_product_telemetry.py tests/core/test_code_context.py tests/infra/code_intel/scip/test_scip_adapter.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_tool_handlers.py tests/core/capabilities/tool_supervision/test_rich_edit_symbol.py tests/gateway/test_mcp_jsonrpc_e2e.py::test_symbol_edit_descriptor_e2e tests/benchmarks/code_intel/test_cost_discipline.py tests/benchmarks/code_intel/test_symbol_edit_bench.py tests/benchmarks/code_intel/test_usages_bench.py -q` | Passed | ✓ PASS |
| Repo validation matrix gates | `make lint && make typecheck && make docs-check && make check-agent-context` | Passed | ✓ PASS |
| Frontend Overview surface | `cd frontend && npm run build` | Passed | ✓ PASS |
| Frontend tests with redirected tmp | `cd frontend && TMPDIR=/home/pankaj/.copilot/session-state/46df9953-1e9a-4044-b4f7-894b5646ea13/tmp npm run test` | Passed | ✓ PASS |

## Human Verification Required

### 1. ast-grep bootstrap realism check

**Test:** Confirm ast-grep bootstrap and binary discovery on the real developer machine.
**Expected:** The chosen install/discovery path is realistic locally and does not rely on the wrong Linux `sg` binary.
**Why human:** Fixture-backed tests prove behavior, but not real-machine binary availability and installation ergonomics.

### 2. Brownfield shared-surface review

**Test:** Review the Phase 2 diffs in `src/atelier/core/capabilities/code_context/engine.py`, `src/atelier/gateway/adapters/mcp_server.py`, and `src/atelier/core/capabilities/tool_supervision/rich_edit.py`.
**Expected:** The phase extends the existing brownfield surfaces additively rather than replacing them wholesale.
**Why human:** Automated tests do not judge maintainability of the shared landing zones.

### 3. Practical symbol-first workflow check

**Test:** Perform a real workflow that finds code structurally, edits a named symbol, and inspects usages.
**Expected:** The workflow stays symbol-first and does not require line-number or grep-first fallbacks.
**Why human:** Benchmarks validate token cost and response shape, not operator UX.

## Gaps Summary

No remaining code-level must-have gaps were found for `DISC-01`, `DISC-02`, `NAVG-02`, or the M12 cache-hit-rate telemetry closeout. Phase status remains `human_needed` solely because the planned manual/UAT checks and explicit final approval have not yet been recorded.

---

_Verified: 2026-05-19T07:06:49Z_
_Verifier: the agent (gsd-verifier, final rerun after telemetry fix)_
