---
phase: 01-retrieval-core-routed-symbol-search
verified: 2026-05-18T21:23:14Z
status: human_needed
score: 6/6 must-haves verified
overrides_applied: 0
human_verification:
  - test: "Review the diff in src/atelier/core/capabilities/code_context/ and src/atelier/gateway/adapters/mcp_server.py"
    expected: "Phase 1 changes narrow and complete the in-flight brownfield implementation without replacing the existing code-context / MCP surfaces wholesale."
    why_human: "Automated tests prove behavior, but they do not prove brownfield-safe coexistence with the user's in-flight edits."
  - test: "Confirm local SCIP bootstrap assumptions against the actual developer machine/toolchains"
    expected: "Phase 1 works with realistic local Python/TypeScript-friendly SCIP paths and does not rely on unavailable bootstrap tooling such as go-based flows."
    why_human: "The provider is fixture-friendly and fallback-safe, but environment realism for bootstrap/tool discovery cannot be fully verified from static inspection."
  - test: "Exercise an end-to-end agent workflow using default code op=\"search\""
    expected: "Default search results are snippet-free, ranked, and sufficient for symbol-first navigation without needing an immediate fallback to ad hoc text search."
    why_human: "Benchmarks and payload assertions prove token shape/cost, but not whether the default feels navigation-first in real agent usage."
---

# Phase 1: Retrieval Core & Routed Symbol Search Verification Report

**Phase Goal:** Agents can retrieve symbols through existing `code` operations with cache-aware, provenance-aware, budget-packed defaults.
**Verified:** 2026-05-18T21:23:14Z
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Repeat `code` lookups return `cache_hit`, `tokens_saved`, and provenance metadata on the existing `code` tool. | ✓ VERIFIED | `tool_code()` remains the existing MCP tool in `src/atelier/gateway/adapters/mcp_server.py:1914-2001`; engine cache wiring is in `src/atelier/core/capabilities/code_context/engine.py:305-341,1463-1468`; MCP/core assertions pass in `tests/gateway/test_p0_mcp_surfaces.py:56-75`, `tests/gateway/test_mcp_tool_handlers.py:487-540`, `tests/core/test_code_context.py:86-99`; telemetry propagation is asserted in `tests/gateway/test_savings_api.py:935-1100`. |
| 2 | Budget-packed `code` payloads stay within declared token budgets after wrapper metadata is attached. | ✓ VERIFIED | Wrapper-aware packing is implemented in `src/atelier/core/capabilities/code_context/engine.py:1308-1429` and uses `BudgetPacker.pack()` from `src/atelier/core/capabilities/code_context/budget.py:15-74`; budget regressions pass in `tests/core/test_code_context.py:117-175` and `tests/gateway/test_p0_mcp_surfaces.py:98-105`. |
| 3 | Existing `code` lookups can return routed SCIP-backed results when a valid index is available, without adding a new top-level MCP tool. | ✓ VERIFIED | `CodeContextEngine` registers `ScipSymbolIntelProvider` onto `SymbolIntelStore` in `src/atelier/core/capabilities/code_context/engine.py:191-197,1544-1555`; the provider loads repo-local `.scip` artifacts in `src/atelier/infra/code_intel/scip/adapter.py:20-102` and `reader.py:73-140`; MCP surface still exposes only `code` (`src/atelier/gateway/adapters/mcp_server.py:1914-2001`, `tests/gateway/test_mcp_tool_handlers.py:25-39`); routed behavior passes in `tests/infra/code_intel/scip/test_scip_adapter.py:155-220` and `tests/gateway/test_mcp_tool_handlers.py:543-563`. |
| 4 | When no valid SCIP artifact is available, symbol retrieval still falls back to the local engine path and refreshed artifacts invalidate stale cache entries. | ✓ VERIFIED | Store fallback is in `src/atelier/core/capabilities/code_context/intel_store.py:88-144`; artifact refresh bumps engine state in `src/atelier/core/capabilities/code_context/engine.py:1560-1576` through watcher signatures from `src/atelier/infra/code_intel/scip/watcher.py:9-28`; fallback/invalidation tests pass in `tests/infra/code_intel/scip/test_scip_adapter.py:141-220` and `tests/gateway/test_mcp_tool_handlers.py:603-616`. |
| 5 | `code op="search"` provides ranked, hardened symbol search defaults on the existing MCP surface. | ✓ VERIFIED | Additive search params stay on `tool_code(op="search")` in `src/atelier/gateway/adapters/mcp_server.py:1915-1966`; search defaults are snippet-free (`snippet="none"`) and optional snippet fields are droppable in `src/atelier/core/capabilities/code_context/engine.py:45-57,288-339,506-530,1470-1507`; exact-match-first/routed provenance behavior is asserted in `tests/core/test_code_context.py:197-214`, `tests/gateway/test_p0_mcp_surfaces.py:108-140`, and `tests/gateway/test_mcp_tool_handlers.py:565-600`. |
| 6 | Benchmark, trace, and validation artifacts exist to prove the low-token Phase 1 retrieval/search path and close validation. | ✓ VERIFIED | Benchmark harness exists in `src/benchmarks/code_intel/symbol_search_bench.py:12-96` and is exercised by `tests/benchmarks/code_intel/test_symbol_search_bench.py:102-202` including the M1/M2 thresholds; `docs/agent-os/validation-matrix.md:3-16` names the code-intel benchmark gates; `.planning/phases/01-retrieval-core-routed-symbol-search/01-VALIDATION.md:1-85` is closed with `nyquist_compliant: true` and `wave_0_complete: true`; trace query returned milestone hits for `M0-store.md`, `M1-scip-adapter.md`, and `M2-symbol-tool.md`. |

**Score:** 6/6 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
| --- | --- | --- | --- |
| `src/atelier/core/capabilities/code_context/cache.py` | Retrieval cache keying, invalidation compatibility, payload reuse | ✓ VERIFIED | Substantive cache implementation (`RetrievalCache.get/set/make_key`) keyed by args + repo + index version; wired from engine via `_cache_get/_cache_set`. |
| `src/atelier/core/capabilities/code_context/budget.py` | Shared budget packing policy | ✓ VERIFIED | `BudgetPacker.pack()` drops optional keys before removing items; engine uses it in `_fit_items_to_budget()`. |
| `src/atelier/core/capabilities/code_context/intel_store.py` | Routed symbol-intel contract with fallback | ✓ VERIFIED | `SymbolIntelStore.search_symbols()/get_symbol()` route to healthy providers, else local callbacks. |
| `src/atelier/infra/code_intel/scip/adapter.py` | SCIP-backed provider for routed symbol lookups | ✓ VERIFIED | Provider refreshes watcher state, loads trusted artifacts, and returns `scip` results without changing payload shape. |
| `src/atelier/gateway/adapters/mcp_server.py` | Existing `code` MCP tool with additive search params and telemetry propagation | ✓ VERIFIED | `@mcp_tool(name="code")`; `tool_code()` forwards hardened search params; `_record_context_budget_for_tool()` copies `cache_hit` and `provenance`. |
| `src/benchmarks/code_intel/symbol_search_bench.py` | Deterministic Phase 1 benchmark harness | ✓ VERIFIED | `run_symbol_search_bench()` creates a fixture repo, runs uncached/cached searches, and reports token/cache/provenance data. |
| `tests/benchmarks/code_intel/test_symbol_search_bench.py` | Runnable benchmark smoke and threshold gates | ✓ VERIFIED | Covers smoke, JSON serialization, M1 latency/token thresholds, and M2 ≤25%-of-baseline token gate. |
| `.planning/phases/01-retrieval-core-routed-symbol-search/01-VALIDATION.md` | Final validation closure | ✓ VERIFIED | Closed frontmatter plus explicit trace query and benchmark commands. |

### Key Link Verification

| From | To | Via | Status | Details |
| --- | --- | --- | --- | --- |
| `src/atelier/gateway/adapters/mcp_server.py` | `src/atelier/core/capabilities/code_context/engine.py` | `tool_code(op="search"|"symbol"|...)` dispatch | ✓ VERIFIED | `tool_code()` constructs an engine and forwards additive params unchanged (`snippet`, `snippet_lines`, `file_glob`, `scope`, `budget_tokens`). |
| `src/atelier/core/capabilities/code_context/engine.py` | `src/atelier/core/capabilities/code_context/cache.py` | `_cache_get/_cache_set` around existing tool wrappers | ✓ VERIFIED | Search/symbol/context/impact wrappers all read/write `RetrievalCache` using current index version. |
| `src/atelier/core/capabilities/code_context/engine.py` | `src/atelier/core/capabilities/code_context/intel_store.py` | `self.intel_store.search_symbols()` / `.get_symbol()` | ✓ VERIFIED | Engine delegates symbol retrieval through the routed store before local fallback. |
| `src/atelier/core/capabilities/code_context/engine.py` | `src/atelier/infra/code_intel/scip/adapter.py` | `_register_symbol_intel_providers()` registers `ScipSymbolIntelProvider` onto `intel_store` | ✓ VERIFIED | Equivalent to the planned provider-registration link, implemented at engine startup rather than inside `intel_store.py` itself. |
| `src/atelier/infra/code_intel/scip/watcher.py` | `src/atelier/core/capabilities/code_context/engine.py` | artifact signature sync bumps `index_version` | ✓ VERIFIED | `ScipArtifactWatcher.refresh()` calls `state_sync`; engine `_sync_external_artifact_state()` bumps `index_version` on signature changes. |
| `src/atelier/gateway/adapters/mcp_server.py` | savings telemetry | `_record_context_budget_for_tool()` copies cache/provenance/op metadata | ✓ VERIFIED | Metadata propagation is explicit in `mcp_server.py:2377-2383` and covered by savings API tests. |
| `src/benchmarks/code_intel/symbol_search_bench.py` | `tests/benchmarks/code_intel/test_symbol_search_bench.py` | `run_symbol_search_bench()` import/use | ✓ VERIFIED | Benchmark tests call the harness directly and assert cache/provenance/token outputs. |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
| --- | --- | --- | --- | --- |
| `engine.py` | `items` in `tool_search()` | `search_symbols()` → `SymbolIntelStore` → local SQLite FTS rows or loaded SCIP artifacts | SQLite queries (`engine.py:553-571`) and `.scip` artifact parsing (`reader.py:80-119`) both return non-static symbol data | ✓ FLOWING |
| `mcp_server.py` | `result` returned by `tool_code()` | `CodeContextEngine.tool_search()/tool_symbol()/...` | Dynamic engine payloads; direct MCP handler tests assert actual returned fields | ✓ FLOWING |
| `adapter.py` | `_artifacts` / routed symbol payloads | `ScipIndexer.discover_artifacts()` + `ScipArtifactReader.load()` | Uses repo-local `.scip` JSON and repo source slices, not hardcoded placeholder data | ✓ FLOWING |
| `_record_context_budget_for_tool()` | `savings_metadata` | Metadata copied from tool results (`cache_hit`, `provenance`, `op`) | Savings tests observe emitted live events and persisted recorder rows | ✓ FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| --- | --- | --- | --- |
| Phase 1 targeted retrieval/search suite | `uv run pytest tests/core/test_code_context.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_savings_api.py tests/infra/code_intel/scip/test_scip_adapter.py tests/benchmarks/code_intel/test_symbol_search_bench.py -q -x` | `71 passed` | ✓ PASS |
| Phase 1 lint/typecheck gate | `make lint && make typecheck` | `ruff check src` passed; `mypy --strict src` passed | ✓ PASS |
| Milestone trace evidence exists | `uv run python -c "...list_traces(query=milestone)..."` | Returned trace IDs for `M0-store.md`, `M1-scip-adapter.md`, `M2-symbol-tool.md` | ✓ PASS |

### Probe Execution

| Probe | Command | Result | Status |
| --- | --- | --- | --- |
| — | — | No documented phase probes or `scripts/*/tests/probe-*.sh` found | ? SKIP |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| --- | --- | --- | --- | --- |
| `FNDN-01` | `01-01-PLAN.md` | Cached, budget-packed existing `code` ops expose `cache_hit`, `tokens_saved`, and provenance metadata. | ✓ SATISFIED | Engine cache/budget wiring in `engine.py`, MCP surface in `mcp_server.py`, regressions in `tests/core/test_code_context.py`, `tests/gateway/test_p0_mcp_surfaces.py`, and `tests/gateway/test_savings_api.py`. |
| `FNDN-02` | `01-02-PLAN.md` | Existing `code` surface can route symbol intelligence through SCIP when available and fall back safely otherwise. | ✓ SATISFIED | Routed provider registration and artifact refresh handling in `engine.py`, `intel_store.py`, `src/atelier/infra/code_intel/scip/*`, plus passing infra/MCP SCIP tests. |
| `NAVG-01` | `01-03-PLAN.md` | Existing `code op="search"` supports hardened symbol-search defaults for snippets, ranking, and provenance. | ✓ SATISFIED | Additive `tool_code` params, exact-match/snippet-free search defaults, provenance breakdown, and benchmark/token-threshold tests. |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| --- | --- | --- | --- | --- |
| `tests/benchmarks/code_intel/test_symbol_search_bench.py` | 123 | Latency benchmark compares warmed routed `search_symbols()` against fresh local `tool_search` + `tool_symbol` workloads | ℹ️ Info | Good evidence of routed advantage, but not strict same-surface parity; speedup claim is directionally strong rather than API-symmetric. |
| `tests/core/test_code_context.py` | 197 | No dedicated assertion of a literal “outline-first” contract beyond exact-match ordering and snippet-free defaults | ℹ️ Info | Automated evidence for the roadmap phrase “outline-first” is indirect; real workflow confirmation is still useful. |
| `src/atelier/core/capabilities/code_context/engine.py` | 958 | Repo path-escape guard exists, but Phase 1 tests do not directly assert rejection behavior | ⚠️ Warning | Safety logic is present; targeted regression coverage for path-escape failures is missing. |

### Human Verification Required

### 1. Brownfield coexistence review

**Test:** Review the diff in `src/atelier/core/capabilities/code_context/` and `src/atelier/gateway/adapters/mcp_server.py`.
**Expected:** The phase narrows/completes the in-flight implementation without replacing the existing brownfield surfaces wholesale.
**Why human:** Automated tests prove behavior, not preservation of a human-maintained brownfield editing strategy.

### 2. SCIP bootstrap realism check

**Test:** Validate the local SCIP bootstrap/index-discovery assumptions on the real developer machine.
**Expected:** The initial routed path works within realistic Python/TypeScript-friendly tooling and does not secretly depend on unavailable bootstrap tooling.
**Why human:** Static inspection confirms fallback-safe code and fixture-driven artifacts, but not local environment reality.

### 3. Default search UX check

**Test:** Perform a symbol-finding task using default `code op="search"` parameters.
**Expected:** Results are ranked, snippet-free by default, and sufficient to navigate without immediately falling back to text search.
**Why human:** Benchmarks and payload tests validate cost/shape, not real operator usability.

### Gaps Summary

No code-level must-have gaps were found. Automated verification confirmed the Phase 1 cache, budget packing, routed SCIP fallback, hardened `code op="search"` surface, benchmark harness, and validation/trace artifacts. Overall status remains `human_needed` because the phase still has explicit manual verification items for brownfield coexistence, bootstrap realism, and end-user default-search usability.

---

_Verified: 2026-05-18T21:23:14Z_
_Verifier: the agent (gsd-verifier)_
