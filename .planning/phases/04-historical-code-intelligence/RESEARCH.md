# Phase 4 Research — Historical Code Intelligence

**Date:** 2026-05-19
**Phase:** 04-historical-code-intelligence
**Requirements:** `HIST-01`, `HIST-02`

## Summary

Phase 4 should be planned as a two-step history substrate:

- `04-01` builds deleted/renamed symbol search plus temporal author/date filters on the existing `code op="search"` surface.
- `04-02` builds `code op="blame"` and churn scoring on top of the same git-history foundation.

The safest brownfield approach is to keep all user-visible changes on `tool_code`, put git-history logic in a new `src/atelier/infra/code_intel/git_history/` package, and treat `engine.py` as orchestration only: schema/accessor wiring, cache keys, filter parsing, stale-index checks, and dispatch.

## Recommended decomposition

### 04-01 — Git-history graveyard for deleted symbols, renames, and temporal filters

- Add a new `src/atelier/infra/code_intel/git_history/` package with:
  - `graveyard.py`
  - `walker.py`
  - `renames.py`
  - `adapter.py`
  - shared models
- Extend `CodeContextEngine` with:
  - a public DB connection/accessor
  - `symbol_graveyard` schema init
  - deleted-scope search dispatch
  - temporal filter parsing/plumbing
  - history-sensitive cache keys
- Add additive `since` and `touched_by` params to `tool_code`, but keep their first full use inside deleted/history search so `04-01` remains self-contained.
- Reuse the existing search envelope (`items`, `cache_hit`, `tokens_saved`, provenance metadata); do not invent a separate public history payload contract.
- Leave bootstrap/background warm integration to later phases; plan only synchronous query-time history behavior here.

### 04-02 — Blame and churn annotations on `code op="blame"` plus live temporal filtering

- Add `op="blame"` and `include_churn` to the existing `tool_code` surface.
- Reuse the same `git_history/` package for blame/churn code.
- Add explicit index-vs-HEAD staleness enforcement before blame.
- Finish live `since` / `touched_by` filtering for normal repo search by deriving a changed-file set from git history and filtering live search results after normal ranking.
- Add cold/hot blame benchmark coverage and, if warranted, fold one history scenario into the aggregate cost-discipline suite.

## Reusable seams

| Seam | Use in Phase 4 |
| --- | --- |
| `tool_code` additive dispatch pattern in `mcp_server.py` | Add `since`, `touched_by`, and `op="blame"` without creating new MCP tools |
| `CodeContextEngine.tool_search()` and search envelope helpers | Preserve the existing public result shape and budget/cache behavior |
| `_init_schema()` in `engine.py` | Add `symbol_graveyard` and blame-cache tables in the engine SQLite DB |
| `RetrievalCache` + existing cache-key discipline | Add history-sensitive cache args only where needed |
| existing engine symbol extraction logic | Reuse/refactor for historical blob parsing instead of duplicating symbol parsing in git-history code |
| `src/benchmarks/code_intel/*` convention | Add graveyard and blame benches in the same fixture-driven style |
| existing `tests/core/test_code_context.py`, `tests/gateway/test_mcp_tool_handlers.py`, and `tests/gateway/test_p0_mcp_surfaces.py` | Extend the already-established engine/MCP surface-contract suites instead of creating ad hoc checks |

## Concrete landing zones

### 04-01

- Production:
  - `src/atelier/gateway/adapters/mcp_server.py`
  - `src/atelier/core/capabilities/code_context/engine.py`
  - optionally `src/atelier/core/capabilities/code_context/intel_store.py` if deleted-scope routing is widened deliberately
  - new `src/atelier/infra/code_intel/git_history/{__init__.py,graveyard.py,walker.py,renames.py,adapter.py,models.py}`
  - `src/atelier/infra/tree_sitter/tags.py` and/or a new extracted symbol-from-text helper
- Tests:
  - `tests/core/test_code_context.py`
  - `tests/gateway/test_mcp_tool_handlers.py`
  - `tests/gateway/test_p0_mcp_surfaces.py`
  - new `tests/infra/code_intel/git_history/` suites
  - `src/benchmarks/code_intel/graveyard_bench.py`
  - `tests/benchmarks/code_intel/test_graveyard_bench.py`

### 04-02

- Production:
  - `src/atelier/gateway/adapters/mcp_server.py`
  - `src/atelier/core/capabilities/code_context/engine.py`
  - new `src/atelier/infra/code_intel/git_history/blame.py`
  - shared git-history models/helpers from `04-01`
  - `src/atelier/infra/code_intel/scip/reader.py` / `adapter.py` if blame freshness needs propagated `index_sha`
- Tests:
  - `tests/infra/code_intel/git_history/test_blame.py`
  - `tests/core/test_code_context.py`
  - `tests/gateway/test_mcp_tool_handlers.py`
  - `tests/gateway/test_p0_mcp_surfaces.py`
  - `src/benchmarks/code_intel/blame_bench.py`
  - `tests/benchmarks/code_intel/test_blame_bench.py`

## Brownfield constraints

- Keep `mcp_server.py` limited to:
  - additive `tool_code` params (`since`, `touched_by`, `include_churn`)
  - one new `if op == "blame"` branch
  - immediate delegation
- Keep `engine.py` orchestration-only:
  - schema init
  - public connection accessor
  - cache args
  - budget packing
  - stale-index checks
  - dispatch into `infra/code_intel/git_history/`
- Do not drag `ContextStore` into runtime code-intel history queries; Phase 4 belongs in the engine SQLite DB, not the global product store.
- Do not scope-creep M11 background bootstrap into Phase 4 execution.
- Expect SCIP fixture churn if blame staleness requires `index_sha`; plan that blast radius explicitly.

## Key defaults and decisions

### Public surface

- No new MCP tool.
- Deleted history stays on `code op="search"` with `scope="deleted"`.
- Blame stays on `code op="blame"`.

### History persistence

- Store history data in the existing engine SQLite DB.
- Prefer new tables (`symbol_graveyard`, `symbol_blame_cache`) over a second DB or a global service store.

### Git backend

- Plan around `pygit2`, as the milestone docs specify.
- Make dependency/bootstrap work explicit in the plan because `pygit2` is not currently importable in the local interpreter.
- Do not hide that gap behind shell-parsing fallback logic.

### Provider routing decision

- Recommended default: branch `engine.tool_search()` for `scope="deleted"` unless the team deliberately widens `SymbolIntelStore` and its result contracts for deleted-history payloads.
- Reason: current live-provider contracts and result models are shaped for present-code results, while deleted-history entries need different fields.

### Blame freshness

- Plan an explicit `index_sha` / HEAD freshness mechanism up front.
- Recommended default: persist an index SHA in engine state and propagate it through SCIP metadata/tests if blame depends on provider-backed symbol byte ranges.

### Local edits

- Recommended default from the milestone: keep blame useful with `local_edits=True` style metadata rather than hard-failing on uncommitted working-tree changes.

## Risks

| Risk | Why it matters | Planning default |
| --- | --- | --- |
| `pygit2` is not currently importable | Phase 4’s chosen git-history backend will not work unless dependency/bootstrap is planned explicitly | Make dependency setup and environment verification part of Wave 1 |
| Deleted-search response shape drift | M14 examples use `matches`, but existing search callers expect the normal `items` envelope | Keep the public search envelope stable and only vary item fields |
| Historical blob parsing currently depends on working-tree file paths | Deleted blobs cannot be parsed by helpers that only re-read files from disk | Extract/add a source-text parsing helper before the walker lands |
| History provider refresh could become too expensive | Full-history walks on normal `code` calls would be unacceptable | Keep refresh limited to cheap HEAD/checkpoint checks, not a full walk |
| Blame without index freshness can point at wrong lines | M15 explicitly requires stale-index enforcement | Plan index-vs-HEAD checks before blame wiring |

## Validation strategy

### Quick automated run

```bash
uv run pytest tests/core/test_code_context.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py -k 'deleted or blame or temporal or churn' -x
```

### Planned additions

- New real-git fixture tests under `tests/infra/code_intel/git_history/` for:
  - delete detection
  - rename detection
  - `since` filter
  - `touched_by` filter
  - blame author/age/sha
  - churn scoring
  - blame cache hits
  - stale-index behavior
- Engine-level integration coverage in `tests/core/test_code_context.py`
- MCP surface coverage in `tests/gateway/test_mcp_tool_handlers.py` and `tests/gateway/test_p0_mcp_surfaces.py`
- Shared fixture updates if `index_sha` becomes required

### Benchmark additions

- `graveyard_bench` for deleted-search latency/token cost versus a manual archaeology baseline
- `blame_bench` for cold vs hot blame
- optionally extend `cost_discipline.py` with one historical-search and one blame scenario

## Planning notes

- Reuse the existing `code` search envelope and budget packer; historical search should feel like another code-intel mode, not a different tool family.
- Keep git-history logic isolated under `src/atelier/infra/code_intel/git_history/`.
- Prefer real temporary git repos in tests over mocking git internals; this phase needs true rename/delete/blame semantics.
- Make dependency/bootstrap for `pygit2` explicit in the plan instead of assuming the environment already has it.
