---
phase: 06-bootstrap-dependency-scope-multi-repo-workspaces
verified: 2026-05-19T23:19:28Z
status: human_needed
score: 6/6 must-haves verified
overrides_applied: 0
human_verification:
  - test: "Call `context` twice against a fixture repo after clearing prior bootstrap state."
    expected: "The second response should include warmed `bootstrap/<repo_id>/...` content without queueing a duplicate bootstrap job."
    why_human: "Automated tests prove enqueue/reuse behavior, but only a human can judge whether the warmed context is actually useful in-session."
  - test: "Query a known dependency symbol with `code op=\"search\" scope=\"external\"` and attempt `edit kind=\"symbol\"` on it."
    expected: "The payload should clearly communicate external origin, and the edit rejection should be understandable/actionable."
    why_human: "Automation verifies fields and rejection codes, not whether the payload wording is clear to maintainers/agents."
  - test: "Run one workspace search with no `repo` filter and one with `repo=\"...\"` in a configured multi-repo workspace."
    expected: "Merged results should be easy to disambiguate via `repo_name`, and the filtered call should clearly narrow to the selected repo."
    why_human: "Automation proves routing and metadata, but humans must confirm the repo-aware payload shape is understandable."
---

# Phase 6: Bootstrap, Dependency Scope & Multi-Repo Workspaces Verification Report

**Phase Goal:** Agents start with warmed code-intel context and can route searches across dependency and workspace boundaries.
**Verified:** 2026-05-19T23:19:28Z
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
| --- | --- | --- | --- |
| 1 | First workspace context bootstraps and prefetches the most relevant code-intel state so later retrieval-heavy sessions start warm. | ✓ VERIFIED | `src/atelier/gateway/adapters/mcp_server.py:677-796` enqueues/starts bootstrap on `tool_get_context`; `src/atelier/core/service/worker.py:50-72` dispatches `bootstrap_context`; `src/atelier/core/runtime/engine.py:111-173` injects persisted bootstrap blocks; tests `tests/gateway/test_mcp_tool_handlers.py:368-443`; spot-check: 17 targeted tests passed plus benchmark trace `20260519T231827-gsd-executor-7d0b2661`. |
| 2 | Agent can distinguish external dependency symbols from workspace symbols in results, and symbol-edit flows reject external targets cleanly. | ✓ VERIFIED | `src/atelier/core/capabilities/code_context/models.py:25-48` adds typed `origin`; `src/atelier/infra/code_intel/scip/adapter.py:66-89` filters repo vs external artifacts by scope; `src/atelier/core/capabilities/tool_supervision/symbol_edit.py:45-57` rejects `origin="external"` edits; tests `tests/core/test_code_context.py:725-744` and `tests/gateway/test_mcp_tool_handlers.py:672-721`; spot-check: 12 targeted tests passed plus benchmark trace `20260519T231825-gsd-executor-a17abbc8`. |
| 3 | Agent can search and resolve code intelligence across supported multi-repo workspaces with repo-aware results and filters. | ✓ VERIFIED | `src/atelier/core/capabilities/code_context/workspace_config.py:30-74` parses `.atelier/workspace.toml`; `src/atelier/core/capabilities/code_context/workspace_router.py:37-120` fans out search/symbol and annotates `repo_name`; `src/atelier/gateway/adapters/mcp_server.py:2043-2178` adds additive `repo` routing; tests `tests/core/test_code_context_workspace.py:36-194` and `tests/gateway/test_mcp_tool_handlers.py:724-775`; spot-check: 10 targeted tests passed plus benchmark trace `20260519T231826-gsd-executor-5b6e9698`. |
| 4 | Partial or failed bootstrap work is visible and retryable through the existing job lifecycle instead of silently rerunning on every context call. | ✓ VERIFIED | `src/atelier/gateway/adapters/mcp_server.py:677-711` exposes bootstrap status and missing labels; `src/atelier/core/foundation/store.py:1194-1260` reclaims `failed` jobs and transitions them to `failed`/`dead`; `tests/core/service/test_bootstrap_context.py:85-107` verifies partial metadata/reuse. |
| 5 | `code op="search"` keeps `scope="repo"` as the default and does not surface dependency hits unless `scope="external"` is requested. | ✓ VERIFIED | `src/atelier/core/capabilities/code_context/intel_store.py:123-143` falls back to local only for `scope="repo"` and returns `[]` for missing external hits; `src/atelier/core/capabilities/code_context/engine.py:1126-1190` routes `scope="external"` explicitly; tests `tests/core/test_code_context.py:700-744` and `tests/gateway/test_mcp_tool_handlers.py:672-690`. |
| 6 | Cross-repo workspace routing stays read-only in this phase and does not widen `edit`, `read`, `smart_search`, or Zoekt semantics. | ✓ VERIFIED | `src/atelier/core/capabilities/code_context/workspace_router.py:16-43` supports only `search` and `symbol`; `src/atelier/gateway/adapters/mcp_server.py:2093-2098` rejects `repo` filters on unsupported ops; test `tests/gateway/test_p0_mcp_surfaces.py:267-291` confirms rejection. |

**Score:** 6/6 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
| --- | --- | --- | --- |
| `src/atelier/core/service/bootstrap_context.py` | deterministic bootstrap planning and persistence | ✓ VERIFIED | Builds `bootstrap/<repo_id>/...` labels, persists/reuses pinned blocks, renders warm context. |
| `src/atelier/core/service/jobs.py` | bootstrap job type ownership | ✓ VERIFIED | Defines `JOB_BOOTSTRAP_CONTEXT` and exports it in `KNOWN_JOB_TYPES`. |
| `src/atelier/core/service/worker.py` | worker dispatch for bootstrap jobs | ✓ VERIFIED | Registers `bootstrap_context_handler` that calls `persist_bootstrap_plan()`. |
| `src/atelier/core/runtime/engine.py` | later-session bootstrap injection | ✓ VERIFIED | Reads bootstrap blocks from memory store and includes them in `get_context()` payloads. |
| `src/benchmarks/code_intel/bootstrap_prefetch_bench.py` | M11 validation evidence | ✓ VERIFIED | Exercises cold/warm `context` flow and records a trace id. |
| `src/atelier/infra/code_intel/scip/external_artifacts.py` | external SCIP discovery rules | ✓ VERIFIED | Classifies and discovers `external-*.scip` under the existing cache root. |
| `src/atelier/core/capabilities/code_context/models.py` | additive origin/repo metadata | ✓ VERIFIED | `SymbolRecord` carries `origin`; `SymbolRecord`/`UsageReference` carry `repo_name`. |
| `src/atelier/core/capabilities/tool_supervision/symbol_edit.py` | external-target rejection | ✓ VERIFIED | Raises `external_symbol_edit_not_allowed` before any file mutation/read path. |
| `src/benchmarks/code_intel/external_scope_bench.py` | M9 validation evidence | ✓ VERIFIED | Proves repo-default exclusion, external lookup, and edit rejection. |
| `src/atelier/core/capabilities/code_context/workspace_config.py` | workspace config parsing | ✓ VERIFIED | Validates `.atelier/workspace.toml`, relative paths, and repo name uniqueness. |
| `src/atelier/core/capabilities/code_context/workspace_router.py` | per-repo fan-out and merge/filter | ✓ VERIFIED | Routes supported ops across configured repos and annotates `repo_name`. |
| `src/atelier/gateway/adapters/mcp_server.py` | additive `repo` routing on `tool_code` | ✓ VERIFIED | Keeps gateway thin while delegating to the workspace router and bootstrap lifecycle helpers. |
| `src/benchmarks/code_intel/workspace_bench.py` | M10 validation evidence | ✓ VERIFIED | Proves union + repo-filter routing and records trace ownership. |

### Key Link Verification

| From | To | Via | Status | Details |
| --- | --- | --- | --- | --- |
| `mcp_server.py` | `jobs.py` | enqueue bootstrap job from `tool_get_context` | ✓ WIRED | `tool_get_context()` calls `_bootstrap_context_status()`, which enqueues `JOB_BOOTSTRAP_CONTEXT`. |
| `worker.py` | `bootstrap_context.py` | worker dispatch of bootstrap handler | ✓ WIRED | `bootstrap_context_handler()` calls `persist_bootstrap_plan()`. |
| `bootstrap_context.py` | `engine.py` | persisted `bootstrap/<repo_id>/...` blocks reused by later sessions | ✓ WIRED | Runtime `get_context()` reads bootstrap blocks rendered by `render_bootstrap_context()`. |
| `indexer.py` | `external_artifacts.py` | discovery of `external-*.scip` files | ✓ WIRED | `discover_artifacts()` merges `discover_external_scip_artifacts()` results with internal artifacts. |
| `engine.py` | `intel_store.py` | explicit `scope="external"` routing while keeping repo default | ✓ WIRED | `search_symbols()` passes `scope` through to `intel_store.search_symbols()`. |
| `symbol_edit.py` | `engine.py` | origin-aware symbol payload validation before edits | ✓ WIRED | `resolve_symbol_edit()` resolves via `CodeContextEngine.get_symbol()` then guards on `origin`. |
| `mcp_server.py` | `workspace_router.py` | optional repo-aware delegation for workspace-scoped code ops | ✓ WIRED | `tool_code()` routes `search`/`symbol` through `_workspace_code_router()`. |
| `workspace_router.py` | `workspace_config.py` | parsed `.atelier/workspace.toml` repo definitions | ✓ WIRED | Router loads `WorkspaceConfig` and uses named repos for fan-out/filtering. |
| `workspace_router.py` | `models.py` | additive `repo_name` result shaping while preserving hashed `repo_id` | ✓ WIRED | Router adds `repo_name` metadata only, leaving `repo_id` untouched. |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
| --- | --- | --- | --- | --- |
| `src/atelier/core/service/bootstrap_context.py` | `block_values` / persisted `MemoryBlock.value` | `CodeContextEngine.repo_map()`, `file_outline()`, `iter_source_files()`, then `memory_store.upsert_block()`; consumed by `AtelierRuntimeCore.get_context()` | Yes — derived from indexed repo files and stored bootstrap memory, not hardcoded placeholders | ✓ FLOWING |
| `src/atelier/infra/code_intel/scip/reader.py` + `adapter.py` | `symbol.origin` and routed symbol payloads | Trusted JSON artifact load in `ScipArtifactReader.load(..., origin=...)` -> `ScipSymbolIntelProvider.search_symbols()/get_symbol()` -> `CodeContextEngine` | Yes — source is parsed SCIP artifact content from repo-local cache files | ✓ FLOWING |
| `src/atelier/core/capabilities/code_context/workspace_router.py` | merged `items` with `repo_name` | `load_workspace_config()` repo definitions -> per-repo `engine.tool_search()/tool_symbol()` payloads -> merged/filter results | Yes — source is live per-repo engine output, not static merged fixtures in production code | ✓ FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| --- | --- | --- | --- |
| Bootstrap targeted suites | `uv run pytest tests/core/service/test_bootstrap_context.py tests/gateway/test_mcp_tool_handlers.py tests/benchmarks/code_intel/test_bootstrap_prefetch_bench.py -k "bootstrap or get_context or warm_context" -q` | `17 passed, 78 deselected` | ✓ PASS |
| External-scope targeted suites | `uv run pytest tests/infra/code_intel/scip/test_scip_adapter.py tests/core/test_code_context.py tests/gateway/test_mcp_tool_handlers.py tests/benchmarks/code_intel/test_external_scope_bench.py -k "external or origin or scope or symbol_edit" -q` | `12 passed, 65 deselected` | ✓ PASS |
| Workspace targeted suites | `uv run pytest tests/core/test_code_context_workspace.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py tests/benchmarks/code_intel/test_workspace_bench.py -k "workspace or repo_name or repo_filter" -q` | `10 passed, 42 deselected` | ✓ PASS |
| Bootstrap benchmark smoke | `uv run python -c "from benchmarks.code_intel.bootstrap_prefetch_bench import run_bootstrap_prefetch_bench; result = run_bootstrap_prefetch_bench(); print(result.trace_id)"` | `20260519T231827-gsd-executor-7d0b2661` | ✓ PASS |
| External benchmark smoke | `uv run python -c "from benchmarks.code_intel.external_scope_bench import run_external_scope_bench; result = run_external_scope_bench(); print(result.trace_id)"` | `20260519T231825-gsd-executor-a17abbc8` | ✓ PASS |
| Workspace benchmark smoke | `uv run python -c "from benchmarks.code_intel.workspace_bench import run_workspace_bench; result = run_workspace_bench(); print(result.trace_id)"` | `20260519T231826-gsd-executor-5b6e9698` | ✓ PASS |

### Probe Execution

| Probe | Command | Result | Status |
| --- | --- | --- | --- |
| None documented/found | n/a | No `scripts/**/tests/probe-*.sh` files or phase-declared probes for Phase 6 | ? SKIP |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| --- | --- | --- | --- | --- |
| `ENBL-01` | `06-01` | Agent gets first-context bootstrap and prefetch behavior that warms the most relevant code-intel state before the first retrieval-heavy task. | ✓ SATISFIED | Bootstrap enqueue/injection flow in `mcp_server.py`, `worker.py`, `bootstrap_context.py`, `runtime/engine.py`; targeted tests and benchmark pass. |
| `DISC-05` | `06-02` | Agent can distinguish external dependency symbols from workspace symbols in code search results. | ✓ SATISFIED | `origin` metadata in `models.py`, external scope routing in `engine.py`/`intel_store.py`/SCIP adapter, edit rejection in `symbol_edit.py`; targeted tests and benchmark pass. |
| `NAVG-04` | `06-03` | Agent can search and resolve code intelligence across supported multi-repo workspaces with repo-aware results. | ✓ SATISFIED | Workspace parser/router plus additive `repo` filter and `repo_name` metadata in `workspace_config.py`, `workspace_router.py`, and `mcp_server.py`; targeted tests and benchmark pass. |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| --- | --- | --- | --- | --- |
| None | — | No Phase 6 blocker markers (`TBD`/`FIXME`/`XXX`) or user-visible stub patterns found in reviewed implementation files. | ℹ️ Info | No anti-pattern blockers identified. |

### Disconfirmation Notes

- **Partial requirement:** Human-facing clarity is not automatically proven for warmed bootstrap content, external-origin payload wording, or `repo_name` disambiguation. This is why status remains `human_needed` even though the code paths are implemented.
- **Misleading passing test:** The `*_is_json_serializable_and_records_trace` benchmark tests (`tests/benchmarks/code_intel/test_bootstrap_prefetch_bench.py:11-18`, `tests/benchmarks/code_intel/test_external_scope_bench.py:11-18`, `tests/benchmarks/code_intel/test_workspace_bench.py:11-18`) prove serialization/trace presence, not the full milestone behavior. The behavior is covered by the companion benchmark assertions and targeted gateway/core tests, not by those serialization checks alone.
- **Uncovered error path:** I found no direct automated test that a failed bootstrap job is reclaimed from `failed` and retried until `dead`; the behavior is inferable from `src/atelier/core/foundation/store.py:1194-1260`, but failure/retry cycling is not exercised explicitly in Phase 6 tests.

## Human Verification Required

### 1. Warm second-session context reuse

**Test:** Call `context` twice against a fixture repo after clearing prior bootstrap state.  
**Expected:** The second response should include warmed bootstrap content without queueing duplicate bootstrap work.  
**Why human:** Tests verify reuse mechanics, but only a human can judge whether the warmed context is materially useful.

### 2. External dependency payload clarity

**Test:** Query a known dependency symbol with `scope="external"` and then attempt a symbol edit on it.  
**Expected:** The response should clearly communicate external origin, and the edit rejection should be understandable/actionable.  
**Why human:** Automation checks fields and error codes, not wording quality.

### 3. Multi-repo disambiguation clarity

**Test:** Run one workspace search without `repo` and one with `repo="billing"` (or another configured repo).  
**Expected:** `repo_name` should make merged hits easy to distinguish, and the filtered call should clearly narrow to the selected repo.  
**Why human:** Automation proves routing, but humans must evaluate payload readability/usability.

## Closeout Notes

- `.planning/PROJECT.md:131` is stale and still says `Next up: Phase 6`. For closeout consistency it should be updated to point at Phase 7, matching `.planning/ROADMAP.md`.

## Gaps Summary

No blocking code or wiring gaps were found for the Phase 6 goal. Automated verification passed for bootstrap warming, external dependency scope handling, and multi-repo workspace routing. Remaining follow-up is human UAT for usability/clarity plus the stale `.planning/PROJECT.md` phase pointer.

---

_Verified: 2026-05-19T23:19:28Z_  
_Verifier: the agent (gsd-verifier)_
