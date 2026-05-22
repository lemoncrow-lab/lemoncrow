---
phase: 03-semantic-recall-relationship-navigation
verified: 2026-05-19T10:04:22Z
status: human_needed
score: 3/3 requirements verified
reverification: true
overrides_applied: 0
human_verification:
  - test: "Review brownfield coexistence in mcp_server.py and engine.py"
    expected: "Semantic ranking, recall assembly, and call-graph traversal stay in helpers while the shared hotspots remain thin and additive."
    why_human: "Automated tests prove behavior, not whether the brownfield landing zones stayed maintainable."
  - test: "Exercise an intent-first workflow across the shipped Phase 3 surfaces"
    expected: "A natural-language symbol search can flow into recall_symbol and then callers/callees without grep-first or line-number fallback."
    why_human: "Benchmarks and regressions do not prove operator UX."
  - test: "Confirm degraded call-edge mode is explicit to an operator"
    expected: "When call-edge data is absent, callers/callees return structured empty or unavailable output with no invented live-LSP fallback language."
    why_human: "Fixture tests prove payload shape, but not operator-facing clarity."
---

# Phase 3: Semantic Recall & Relationship Navigation Verification Report

**Phase Goal:** Agents can recover intent, prior context, and symbol relationships before they change code.
**Verified:** 2026-05-19T10:04:22Z
**Status:** human_needed
**Re-verification:** Yes — rerun after fixing the strict-mypy blocker in `symbol_recall.py`.

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
| --- | --- | --- | --- |
| 1 | `code op="search"` now supports semantic and hybrid ranking on the existing MCP surface without displacing exact-name lexical behavior. | ✓ VERIFIED | `src/atelier/core/capabilities/code_context/embedding.py`; `src/atelier/core/capabilities/code_context/engine.py`; `src/atelier/gateway/adapters/mcp_server.py`; `tests/core/test_code_context.py`; `tests/gateway/test_p0_mcp_surfaces.py`; `tests/benchmarks/code_intel/test_symbol_search_bench.py`. |
| 2 | `memory op="recall_symbol"` returns a low-token definition-plus-memory bundle by default and widens to traces, decisions, and tests only when explicitly requested. | ✓ VERIFIED | `src/atelier/core/capabilities/archival_recall/symbol_recall.py`; `src/atelier/gateway/adapters/mcp_server.py`; `tests/core/capabilities/archival_recall/test_symbol_recall.py`; `tests/gateway/test_mcp_memory_tools.py`; `tests/benchmarks/code_intel/test_recall_symbol_bench.py`. |
| 3 | `code op="callers"` and `code op="callees"` traverse routed SCIP call edges on the existing `code` surface, keep `depth=1` / `snapshot=False` cheap by default, and return structured unavailable behavior when call-edge data is absent. | ✓ VERIFIED | `src/atelier/core/capabilities/code_context/call_graph.py`; `src/atelier/core/capabilities/code_context/engine.py`; `src/atelier/core/capabilities/code_context/intel_store.py`; `src/atelier/infra/code_intel/scip/reader.py`; `src/atelier/infra/code_intel/scip/adapter.py`; `tests/core/test_code_context.py`; `tests/infra/code_intel/scip/test_scip_adapter.py`; `tests/gateway/test_p0_mcp_surfaces.py`; `tests/benchmarks/code_intel/test_call_graph_bench.py`. |

**Score:** 3/3 requirement truths verified

## Requirements Coverage

| Requirement | Status | Evidence |
| --- | --- | --- |
| `DISC-03` | ✓ SATISFIED | Mode-aware semantic/hybrid ranking is implemented inside the existing `code op="search"` path with exact-name protection, routed-safe cache keys, MCP surface coverage, and benchmark evidence. |
| `DISC-04` | ✓ SATISFIED | `memory op="recall_symbol"` is implemented on the existing memory surface with helper-based bundle assembly, strict-mypy clean type gates, targeted MCP coverage, and dedicated benchmark evidence. |
| `NAVG-03` | ✓ SATISFIED | Routed call-edge loading, traversal helpers, additive MCP wiring, cycle-safe traversal, unavailable-mode behavior, and call-graph benchmark coverage are all in place. |

## Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| --- | --- | --- | --- |
| Phase 3 focused verification rerun | `TMPDIR=/home/pankaj/.copilot/session-state/46df9953-1e9a-4044-b4f7-894b5646ea13/tmp uv run pytest tests/core/test_code_context.py tests/core/capabilities/archival_recall/test_symbol_recall.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_mcp_memory_tools.py tests/infra/code_intel/scip/test_scip_adapter.py tests/benchmarks/code_intel/test_symbol_search_bench.py tests/benchmarks/code_intel/test_recall_symbol_bench.py tests/benchmarks/code_intel/test_call_graph_bench.py -q` | Passed (`54 passed`) | ✓ PASS |
| Strict type gate on Phase 3 blocker file | `TMPDIR=/home/pankaj/.copilot/session-state/46df9953-1e9a-4044-b4f7-894b5646ea13/tmp uv run mypy src/atelier/core/capabilities/archival_recall/symbol_recall.py --strict` | Passed | ✓ PASS |

## Non-Blocking Notes

- `docs/plans/active/code-intel/M8-call-graph.md` describes persisted snapshots, while the shipped Phase 3 implementation keeps snapshot behavior as deterministic opt-in metadata only. This does not block `NAVG-03`, but it is a documented milestone deviation worth keeping visible in later roadmap work.
- Focused pytest emitted Pydantic deprecation warnings from site-packages. These warnings do not represent a Phase 3 implementation failure.

## Human Verification Required

### 1. Brownfield coexistence review

**Test:** Review the Phase 3 diffs in `src/atelier/gateway/adapters/mcp_server.py` and `src/atelier/core/capabilities/code_context/engine.py`.
**Expected:** Heavy semantic, recall, and call-graph logic lives in helper modules; the hotspots remain thin and additive.
**Why human:** Automated tests do not judge maintainability of the shared landing zones.

### 2. Intent-first operator workflow

**Test:** Run a natural-language `code op="search"` query, follow it with `memory op="recall_symbol"`, and then inspect `code op="callers"` or `op="callees"` for the resolved symbol.
**Expected:** The workflow stays on the existing Phase 3 MCP surfaces without grep-first or line-number fallback.
**Why human:** Benchmarks validate cost and correctness, not operator experience.

### 3. Degraded call-edge-mode clarity

**Test:** Run callers/callees against a repo or fixture without routed call-edge data.
**Expected:** The response is explicitly empty/unavailable and does not pretend a live-LSP fallback exists.
**Why human:** Fixture assertions prove shape, but not the clarity of the degraded operator message.

## Gaps Summary

No remaining automated blocker gaps were found after the strict-mypy fix in `symbol_recall.py`. Phase status remains `human_needed` only because the planned manual/UAT checks and final approval have not yet been recorded.

---

_Verified: 2026-05-19T10:04:22Z_
_Verifier: the agent (gsd-verifier, rerun after blocker fix)_
