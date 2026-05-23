---
phase: 05-scale-decision-and-extended-retrieval-reach
verified: 2026-05-19T21:13:11Z
status: human_needed
score: 5/5 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 5/5
  gaps_closed:
    - "Documented Phase 5 M16 validation command passes on the current codebase."
  gaps_remaining: []
  regressions: []
human_verification:
  - test: "Run the shipped `search` path twice against a large repo with the managed Zoekt runtime."
    expected: "The response includes `backend=\"zoekt\"` and `index_age_seconds`, and the payload remains useful to an operator."
    why_human: "Payload usefulness/readability is a UX judgment, not a grep-able invariant."
  - test: "Exercise `code op=\"symbol\"` and `code op=\"usages\"` on a known literal cross-language fixture."
    expected: "`cross_lang_refs`, `edge_kind`, and `confidence` are understandable without obscuring the normal local results."
    why_human: "Automated tests verify structure and correctness, not human readability."
---

# Phase 5: Scale Decision & Extended Retrieval Reach Verification Report

**Phase Goal:** Atelier can make the scale-backend choice explicitly and then extend code intelligence to large repos and supported cross-language edges.
**Verified:** 2026-05-19T21:13:11Z
**Status:** human_needed
**Re-verification:** Yes — after gap closure

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Maintainers have a documented build-vs-integrate decision record before large-repo backend work proceeds. | ✓ VERIFIED | `docs/plans/active/code-intel/M18-bvi-checkpoint.md:137-176` contains the completed matrix, decision memo, and ratified `option-a`; `uv run pytest tests/benchmarks/code_intel/test_scale_decision_eval.py -q` passed (`6 passed`). |
| 2 | The checkpoint records whether the selected backend serves `search`, `code`, or both, and records lifecycle ownership outside per-call `CodeContextEngine` rebuilds. | ✓ VERIFIED | `M18-bvi-checkpoint.md:151-167` records `search_scope=search`, `result_shape=text`, and `lifecycle_owner=session-scoped search backend supervisor...`. |
| 3 | Agent can route large-repo `search` workloads through the validated scale backend and see which backend served the result. | ✓ VERIFIED | The exact documented M16 validation row now passes end-to-end: `docs/agent-os/validation-matrix.md:12` command succeeded (`20 passed, 11 deselected`) and the trace command printed `20260519T211143-gsd-executor-8d67f874`; a live managed-runtime probe on this repo returned `should_route=true`, `health.ok=true`, `backend=\"zoekt\"`, `index_age_seconds=0`, `match_count=3`. |
| 4 | Name-first `code op="search"` stays on the existing local/SCIP/semantic path, and the M16 work stays out of `engine.py` / `mcp_server.py`. | ✓ VERIFIED | `tests/gateway/test_p0_mcp_surfaces.py:107-134` still asserts no `backend` field on `tool_code(op=\"search\")`; current diff stats for Phase 5 files exclude `src/atelier/core/capabilities/code_context/engine.py` and `src/atelier/gateway/adapters/mcp_server.py`; `rg` found `0` Zoekt refs in both files while `smart_search.py:209-231` owns backend routing. |
| 5 | Agent can see supported cross-language references with confidence scoring on symbol and usage results for the planned Python/C, subprocess, and dynamic-import cases. | ✓ VERIFIED | `src/atelier/core/capabilities/code_context/models.py:10-46,70-82` defines additive `cross_lang_refs`, `edge_kind`, and `confidence`; `engine.py:670-737,1558-1578` hydrates them; `tests/core/test_code_context.py:790-819` asserts additive symbol/usages output; the Phase 5 cross-language suite passed (`9 passed, 26 deselected`). |

**Score:** 5/5 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
| --- | --- | --- | --- |
| `docs/plans/active/code-intel/M18-bvi-checkpoint.md` | Completed evaluation matrix, repo-specific memo, gate result | ✓ VERIFIED | Matrix, repo-specific answers, decision, and ratification are present. |
| `src/benchmarks/code_intel/scale_decision_eval.py` | Executable M18 rubric runner | ✓ VERIFIED | Backed by passing `test_scale_decision_eval.py`. |
| `src/atelier/infra/code_intel/zoekt/binary.py` | Managed real-runtime activation path | ✓ VERIFIED | Resolves managed Docker runtime from `VERSIONS.toml` and writes `.atelier/bin/MANIFEST.json`. |
| `src/atelier/infra/code_intel/zoekt/server.py` | Real Zoekt runtime lifecycle + bridge | ✓ VERIFIED | Starts the managed Zoekt runtime once per workspace and reports health/index age. |
| `src/atelier/infra/code_intel/zoekt/client.py` | Real Zoekt match parsing | ✓ VERIFIED | Preserves byte offsets consumed by existing snippet shaping. |
| `src/atelier/core/capabilities/tool_supervision/smart_search.py` | Threshold-based routing on existing `search` stack | ✓ VERIFIED | `smart_search.py:209-231` routes large repos through the Zoekt supervisor and falls back cleanly. |
| `tests/gateway/test_p0_mcp_surfaces.py` | Public contract coverage for additive backend metadata and unchanged `code op="search"` | ✓ VERIFIED | `test_mcp_search_adds_backend_metadata_for_large_repo()` now clears `ATELIER_ZOEKT_BIN*`, requires Docker, and asserts the managed runtime path at `tests/gateway/test_p0_mcp_surfaces.py:30-53`. |
| `docs/agent-os/validation-matrix.md` | Passing documented Phase 5 validation command | ✓ VERIFIED | The M16 row at `docs/agent-os/validation-matrix.md:12` matches a passing command on the current codebase. |
| `src/atelier/core/capabilities/code_context/engine.py` | Additive cross-language hydration only | ✓ VERIFIED | Hydration is additive and confined to symbol/usages responses. |

### Key Link Verification

| From | To | Via | Status | Details |
| --- | --- | --- | --- | --- |
| `src/benchmarks/code_intel/scale_decision_eval.py` | `docs/plans/active/code-intel/M18-bvi-checkpoint.md` | rubric output and memo appendix | ✓ WIRED | Memo contents align with the tested rubric output. |
| `src/atelier/core/capabilities/tool_supervision/smart_search.py` | `src/atelier/infra/code_intel/zoekt/adapter.py` | large-repo threshold routing | ✓ WIRED | `_search_with_backend()` calls `get_zoekt_supervisor()` and returns shaped search payloads. |
| `src/atelier/infra/code_intel/zoekt/binary.py` | managed Zoekt runtime availability | `VERSIONS.toml` → `.atelier/bin/MANIFEST.json` → Docker image ref | ✓ WIRED | Managed runtime is provisioned and then consumed by the server/supervisor. |
| `src/atelier/infra/code_intel/zoekt/adapter.py` | `src/atelier/infra/code_intel/zoekt/server.py` | shared session-scoped lifecycle owner | ✓ WIRED | `ZoektSupervisor.ensure_started()` reuses the shared server. |
| `src/atelier/infra/code_intel/zoekt/client.py` | `src/atelier/core/capabilities/tool_supervision/search_read.py` | byte offsets into existing search shaping | ✓ WIRED | Parsed offsets populate `Snippet.byte_start` / `byte_end`. |
| `src/atelier/core/capabilities/code_context/engine.py` | `src/atelier/infra/code_intel/cross_lang/edges.py` | additive lookup of stored cross-language edges for `symbol` and `usages` | ✓ WIRED | `_cross_lang_store().query_by_source_symbol/query_by_target_symbol` drives both surfaces. |
| `src/atelier/core/capabilities/code_context/models.py` | `src/atelier/core/capabilities/code_context/engine.py` | optional-key preservation for `cross_lang_refs`, `edge_kind`, `confidence` | ✓ WIRED | `_SYMBOL_OPTIONAL_KEYS` and `_USAGES_OPTIONAL_KEYS` include the new fields. |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
| --- | --- | --- | --- | --- |
| `smart_search.py` | `backend`, `index_age_seconds`, `matches` | `get_zoekt_supervisor()` → `ZoektServer.raw_search()` → managed Zoekt runtime | Yes | ✓ FLOWING |
| `engine.py` | `cross_lang_refs`, usages `edge_kind` / `confidence` | `CrossLangRunner.resolve_all()` → `CrossLangEdgeStore` queries | Yes | ✓ FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| --- | --- | --- | --- |
| M18 checkpoint automation | `uv run pytest tests/benchmarks/code_intel/test_scale_decision_eval.py -q` | `6 passed` | ✓ PASS |
| Exact documented M16 validation gate | `uv run pytest tests/infra/code_intel/zoekt/test_zoekt_routing.py tests/gateway/test_p0_mcp_surfaces.py tests/benchmarks/code_intel/test_zoekt_bench.py -k "zoekt or backend or search" -q && uv run python -c "from benchmarks.code_intel.zoekt_bench import run_zoekt_bench; result = run_zoekt_bench(); print(result.trace_id)"` | `20 passed, 11 deselected`; trace id `20260519T211143-gsd-executor-8d67f874` | ✓ PASS |
| Live managed-runtime probe on this repo | `ATELIER_CACHE_DISABLED=1 ATELIER_ZOEKT_LOC_THRESHOLD=20 uv run python ... smart_search(query="zoekt")` | `should_route=true`, `health.ok=true`, `backend='zoekt'`, `index_age_seconds=0`, `match_count=3` | ✓ PASS |
| Phase 5 cross-language automation | `uv run pytest tests/infra/code_intel/cross_lang/test_edges.py tests/infra/code_intel/cross_lang/test_resolvers.py tests/core/test_code_context.py tests/benchmarks/code_intel/test_cross_lang_bench.py -k "cross_lang or ctypes or cffi or import_module or subprocess" -q` | `9 passed, 26 deselected` | ✓ PASS |
| Zoekt typing/linting on touched files | `uv run mypy --strict src/atelier/infra/code_intel/zoekt src/benchmarks/code_intel/zoekt_bench.py` and `uv run ruff check ...` | `Success: no issues found`; `All checks passed!` | ✓ PASS |

### Probe Execution

| Probe | Command | Result | Status |
| --- | --- | --- | --- |
| None discovered for Phase 5 | — | No declared or conventional `probe-*.sh` files found | ? SKIP |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| --- | --- | --- | --- | --- |
| `ENBL-03` | `05-01` | Documented build-vs-integrate decision record before backend work proceeds | ✓ SATISFIED | M18 memo exists, is ratified, and its tests pass. |
| `SCAL-01` | `05-02` | Route large-repo search workloads through validated scale backend | ✓ SATISFIED | Exact validation-matrix row passes; live managed-runtime probe returns `backend="zoekt"` and non-null `index_age_seconds`; hotspot files remain untouched. |
| `SCAL-02` | `05-03` | Surface supported cross-language edges with confidence scoring | ✓ SATISFIED | Engine/model wiring, targeted tests, and benchmark smoke all passed. |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| --- | --- | --- | --- | --- |
| `tests/infra/code_intel/zoekt/test_zoekt_routing.py` | coverage gap | No explicit test for managed-runtime failure after Docker is discovered but image pull/start fails | ⚠️ Warning | The env-override fallback path is tested, but one managed bootstrap unhappy path remains uncovered. |

### Human Verification Required

### 1. Large-repo payload usefulness

**Test:** Run the shipped `search` path twice against a large repo with the managed Zoekt runtime.  
**Expected:** The response includes `backend="zoekt"` and `index_age_seconds`, and the payload remains useful to an operator.  
**Why human:** Payload usefulness/readability is a UX judgment, not a grep-able invariant.

### 2. Cross-language response readability

**Test:** Exercise `code op="symbol"` and `code op="usages"` on a known literal cross-language fixture.  
**Expected:** `cross_lang_refs`, `edge_kind`, and `confidence` are understandable without obscuring existing local results.  
**Why human:** Automated tests verify structure and correctness, not human readability.

## Gaps Summary

No automated blockers remain. The prior Phase 5 gap is closed: the updated gateway contract test now validates the managed real Zoekt runtime, the exact M16 validation-matrix row passes on the current codebase, the benchmark trace command succeeds, and a live probe on the repository routes large-repo search through `backend="zoekt"` with non-null `index_age_seconds`.

Phase 5 is not marked `passed` only because two manual payload/readability checks remain from the validation plan. There is also one non-blocking warning: managed bootstrap fallback is not directly tested for the specific “Docker exists but pull/start fails” unhappy path.

---

_Verified: 2026-05-19T21:13:11Z_  
_Verifier: the agent (gsd-verifier)_
