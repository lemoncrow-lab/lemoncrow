---
phase: 04-historical-code-intelligence
verified: 2026-05-19T13:35:00Z
status: human_needed
score: 10/10 must-haves verified
reverification: true
overrides_applied: 0
human_verification:
  - test: "Review deleted-history payload usefulness"
    expected: "Deleted or renamed symbol responses stay on the normal `items` envelope and explain rename/deletion metadata clearly enough to avoid manual git archaeology."
    why_human: "Automated tests prove payload correctness, not operator usefulness."
  - test: "Review blame usefulness on stable and churn-heavy symbols"
    expected: "Author, age, and churn fields are clear enough to guide edit-risk decisions without shelling out to git."
    why_human: "Benchmarks prove the fields exist, not that the explanation is decision-useful."
  - test: "Review stale-index remediation clarity"
    expected: "The `index_stale` response gives a clear, actionable reindex hint."
    why_human: "Automated tests verify shape, not operator clarity."
  - test: "Review brownfield hotspot containment in `mcp_server.py` and `engine.py`"
    expected: "`mcp_server.py` remains additive-only and `engine.py` remains orchestration-only, with git-history execution isolated under `src/atelier/infra/code_intel/git_history/`."
    why_human: "Automated tests do not judge maintainability of shared hotspots."
---

# Phase 4: Historical Code Intelligence Verification Report

**Phase Goal:** Agents can reason about deleted code, renames, ownership, and stability before making changes.
**Verified:** 2026-05-19T13:35:00Z
**Status:** human_needed
**Re-verification:** Yes — rerun after fixing the Phase 4 strict-type blocker in `8fa8629`

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Agent can search deleted or renamed symbols and filter historical results by time window or author. | ✓ VERIFIED | `tool_search()` parses `since`/`touched_by` (`engine.py:405-483`), routes `scope="deleted"` into `DeletedHistorySearchAdapter.search()` (`engine.py:1006-1013`, `adapter.py:30-84`), and quick command `pytest ... -k "deleted or graveyard or temporal or touched_by"` passed (16 passed). |
| 2 | Agent can inspect blame and churn metadata for a symbol to judge ownership and stability before editing. | ✓ VERIFIED | `tool_blame()` enforces freshness and delegates to `BlameAnnotator` (`engine.py:485-596`, `blame.py:19-140`); quick command `pytest ... -k "blame or churn or temporal or index_stale"` passed (10 passed). |
| 3 | Phase 4 has an explicit pinned `pygit2` bootstrap path with no hidden fallback. | ✓ VERIFIED | `pyproject.toml` pins `pygit2==1.19.2`; `git_history/__init__.py:12-30` raises `GitHistoryBootstrapError` and explicitly rejects GitPython/subprocess fallback; `uv run python -c "import pygit2"` returned `1.19.2`. |
| 4 | The graveyard substrate exists under `src/atelier/infra/code_intel/git_history/` before public wiring. | ✓ VERIFIED | `graveyard.py`, `walker.py`, `renames.py`, and `models.py` are substantive and `tests/infra/code_intel/git_history/test_graveyard.py` passed (6 passed). |
| 5 | Deleted-history search ships on the existing `code` tool with additive filters only. | ✓ VERIFIED | `mcp_server.py:2019-2040` forwards `scope`, `since`, and `touched_by` only for `op="search"`; gateway surface tests passed (`tests/gateway/test_mcp_tool_handlers.py`, `tests/gateway/test_p0_mcp_surfaces.py`). |
| 6 | M14 benchmark and trace evidence exist for the shipped deleted-history surface. | ✓ VERIFIED | `graveyard_bench.py` and `test_graveyard_bench.py` are present and passing; spot-check returned graveyard provenance with rename target; trace file `/home/pankaj/.atelier/traces/20260519T120244-gsd-executor-02199412.json` exists with passing validation results. |
| 7 | The blame/churn substrate and explicit SCIP freshness metadata exist before public blame wiring. | ✓ VERIFIED | `git_history/blame.py` is substantive; `scip/reader.py:189-225` requires 40-char `index_sha`; `scip/adapter.py:105-108` exposes it; `tests/infra/code_intel/git_history/test_blame.py` + `tests/infra/code_intel/scip/test_scip_adapter.py` passed (10 passed). |
| 8 | `code op="blame"` is an additive extension on the existing `code` tool. | ✓ VERIFIED | `mcp_server.py:2042-2055` adds one blame branch with `include_churn` delegation only; public surface tests confirm additive payload shape. |
| 9 | M15 benchmark, cost-discipline, and trace evidence exist for shipped blame/history behavior. | ✓ VERIFIED | `blame_bench.py`, `cost_discipline.py`, and their tests passed; spot-check returned blame provenance, cache hit on second call, and churn data; trace file `/home/pankaj/.atelier/traces/20260519T123857-gsd-executor-ca2ed203.json` exists with three passing validation gates. |
| 10 | Phase 4 owned code passes the required repo typecheck gate for end-of-phase verification. | ✓ VERIFIED | `make typecheck` now passes for the repository after `8fa8629`, including the Phase 4 git-history typing seam and downstream `search_symbols()` callers. |

**Score:** 10/10 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
| --- | --- | --- | --- |
| `pyproject.toml` | pinned `pygit2` dependency | ✓ VERIFIED | Contains `pygit2==1.19.2`. |
| `src/atelier/infra/code_intel/git_history/graveyard.py` | SQLite-backed deleted/renamed storage | ✓ VERIFIED | Real schema, upsert, and filtered query logic. |
| `src/atelier/infra/code_intel/git_history/walker.py` | `pygit2` history walk over deleted/renamed blobs | ✓ VERIFIED | Walks commits, loads parent blobs, extracts tags from source text, upserts graveyard rows. |
| `src/atelier/infra/code_intel/git_history/adapter.py` | deleted-history search adapter | ✓ VERIFIED | Wired from engine; resolves rename targets and changed-file filters from git history. |
| `src/atelier/infra/code_intel/git_history/blame.py` | blame/churn aggregation | ✓ VERIFIED | Uses `pygit2.blame()`, churn scan, cache, and local-edits metadata. |
| `src/atelier/core/capabilities/code_context/engine.py` | search/blame orchestration | ✓ VERIFIED | Runtime behavior, strict typing, and targeted Phase 4 tests all pass after the deleted-history type-boundary fix. |
| `src/atelier/gateway/adapters/mcp_server.py` | additive MCP wiring | ✓ VERIFIED | Search params and one blame branch only; immediate delegation preserved. |
| `src/atelier/infra/code_intel/scip/reader.py` | freshness metadata loading | ✓ VERIFIED | Rejects missing/malformed `index_sha`. |
| `src/benchmarks/code_intel/graveyard_bench.py` | M14 benchmark evidence | ✓ VERIFIED | Public-surface benchmark with manual archaeology baseline. |
| `src/benchmarks/code_intel/blame_bench.py` | M15 benchmark evidence | ✓ VERIFIED | Public-surface blame benchmark with manual git baseline. |

### Key Link Verification

| From | To | Via | Status | Details |
| --- | --- | --- | --- | --- |
| `mcp_server.py` | `engine.py` | additive `scope="deleted"` / `since` / `touched_by` delegation | ✓ WIRED | `tool_code()` search branch forwards params directly (`mcp_server.py:2019-2040`). |
| `engine.py` | `git_history/adapter.py` | deleted-history dispatch and live changed-file filtering | ✓ WIRED | `search_symbols(... scope="deleted")` and repo temporal filter both call `_deleted_history_adapter()` (`engine.py:467-472`, `1006-1013`, `2638-2647`). |
| `mcp_server.py` | `engine.py` | additive `op="blame"` + `include_churn` | ✓ WIRED | `tool_code()` blame branch delegates immediately (`mcp_server.py:2042-2055`). |
| `engine.py` | `git_history/blame.py` | stale-index-checked blame orchestration | ✓ WIRED | `tool_blame()` gates on `index_sha != head_sha`, then instantiates `BlameAnnotator` (`engine.py:535-596`). |
| `scip/reader.py` | `scip/adapter.py` | propagated `index_sha` freshness metadata | ✓ WIRED | `LoadedScipArtifact.index_sha` is required at load and exposed through `ScipSymbolIntelProvider.index_sha()`. |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
| --- | --- | --- | --- | --- |
| `engine.tool_search(scope="deleted")` | `items` | `DeletedHistorySearchAdapter.search()` → `SymbolGraveyard` / `walk_history()` → real git commits and deleted blobs | Yes | ✓ FLOWING |
| `engine.tool_search(scope="repo", since/touched_by)` | filtered `items` | live ranked symbol hits filtered by `DeletedHistorySearchAdapter.changed_files()` over real git walk | Yes | ✓ FLOWING |
| `engine.tool_blame()` | blame payload | routed/local symbol target → `index_sha` freshness → `BlameAnnotator.annotate()` → `pygit2.blame()` + churn scan | Yes | ✓ FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| --- | --- | --- | --- |
| Deleted-history rename-aware search works end to end | `uv run python -c "from pathlib import Path; from benchmarks.code_intel.graveyard_bench import run_graveyard_bench; print(run_graveyard_bench(Path('$TMPDIR')).to_dict())"` | Returned `rename_target='modern.py'`, `uncached_provenance='graveyard'`, `cached_cache_hit=True` | ✓ PASS |
| Blame returns ownership/churn and cache reuse | `uv run python -c "from pathlib import Path; from benchmarks.code_intel.blame_bench import run_blame_bench; print(run_blame_bench(Path('$TMPDIR')).to_dict())"` | Returned `last_author='carol@example.com'`, `churn_commit_count=2`, hot call `cache_hit=True` | ✓ PASS |
| Phase 4 targeted validation suites pass | See 04-VALIDATION quick commands | 04-01: 6 passed; 04-02: 16 passed; 04-03: 10 passed; 04-04: 10 passed | ✓ PASS |
| Repo typecheck gate | `make typecheck` | Passed (`Success: no issues found in 302 source files`) | ✓ PASS |
| Repo test gate | `uv run pytest -x -q` | First failure is `tests/benchmarks/code_intel/test_call_graph_bench.py` (Phase 3 call graph), not a Phase 4 path | ℹ️ NOT A PHASE-4 BLOCKER |

### Probe Execution

| Probe | Command | Result | Status |
| --- | --- | --- | --- |
| none declared / none found | `find scripts -path '*/tests/probe-*.sh' -type f` | no probe scripts found | ? SKIP |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| --- | --- | --- | --- | --- |
| `HIST-01` | `04-01`, `04-02`, `04-04` | Search deleted or renamed symbols and filter historical results by time window or author | ✓ SATISFIED | Deleted search routing, rename-aware adapter behavior, temporal filters, targeted tests, and graveyard benchmark spot-check all passed. |
| `HIST-02` | `04-03`, `04-04` | Inspect blame and churn metadata for ownership/stability judgment | ✓ SATISFIED | Blame substrate, `index_sha` freshness propagation, stale-index response, churn output, targeted tests, and blame benchmark spot-check all passed. |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| --- | --- | --- | --- | --- |
| — | — | No `TODO` / `FIXME` / `XXX` / placeholder markers found in Phase 4-owned files scanned | ℹ️ Info | No stub/debt-marker blocker found by grep scan. |

## Human Verification Required

### 1. Deleted-history operator payload review

**Test:** Run `code op="search"` with `scope="deleted"` for a known deleted or renamed symbol, then repeat with `since` and `touched_by`.
**Expected:** Response keeps the normal `items` envelope and the rename/deletion metadata is understandable without manual git archaeology.
**Why human:** Automated tests prove payload shape, not whether the history explanation is useful to an operator.

### 2. Blame usefulness review

**Test:** Run `code op="blame"` on one stable symbol and one churn-heavy symbol.
**Expected:** Author, age, and churn differences are understandable enough to guide edit-risk decisions.
**Why human:** Benchmarks verify fields exist, not whether the explanation is actually decision-useful.

### 3. Stale-index remediation review

**Test:** Reindex, create a new commit, then run `code op="blame"` on a touched symbol.
**Expected:** Response returns `index_stale` with a clear, actionable reindex hint.
**Why human:** Automated tests verify shape; a human must judge whether the remediation hint is actionable.

### 4. Brownfield hotspot containment review

**Test:** Review final diffs for `src/atelier/gateway/adapters/mcp_server.py` and `src/atelier/core/capabilities/code_context/engine.py`.
**Expected:** `mcp_server.py` remains additive/immediate-delegation-only and `engine.py` remains orchestration-only, with git-history execution staying in `src/atelier/infra/code_intel/git_history/`.
**Why human:** Tests prove behavior, not code-placement discipline.

## Gaps Summary

No automated Phase 4 blocker remains after `8fa8629`. Deleted/renamed search works with temporal filters, blame/churn works with stale-index protection, the repository type gate now passes, the Phase 4-owned pytest slice passes, and both M14/M15 traces exist in `~/.atelier/traces/`.

Broad repo tests still have unrelated earlier-phase red cases, with the first reproduced failure in `tests/benchmarks/code_intel/test_call_graph_bench.py`. That is not a Phase 4 blocker. Phase status is `human_needed` only because the planned manual/UAT checks and final approval have not yet been recorded.

---

_Verified: 2026-05-19T13:35:00Z_
_Verifier: the agent (gsd-verifier, rerun after blocker fix)_
