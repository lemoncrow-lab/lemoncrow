# M0 — Retrieval cache + budget packer inside `code_context`

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Prerequisite for M1–M9. Task: see TaskList.

## Goal

Add two cross-cutting primitives — **content-addressed retrieval cache** and
**token-budget packer** — inside the existing `code_context` capability, then
refactor the existing `tool_code` ops to route results through both. No new
MCP tool. No `infra/code_intel_bridges/` directory (that was speculative from
the abandoned Serena-bridge plan).

## Why first

- Locks the cache key shape and the budget packing policy so every later
  milestone reuses them.
- Gives one chokepoint for telemetry: every `tool_code` call records cache
  hit/miss and tokens-saved (`tool_smart_read` already does this — we
  mirror its shape).
- Means the user-visible cost reduction starts compounding immediately as
  each downstream milestone lands.

## Where the code lives

```
src/atelier/core/capabilities/code_context/
  engine.py            EXISTS — add cache lookup + budget pack to public methods
  cache.py             NEW — RetrievalCache (SQLite-backed, content-addressed)
  budget.py            NEW — BudgetPacker (outline-first packing policy)
  models.py            EXISTS — add provenance field to SymbolRecord
```

No new `infra/` modules. SCIP and ast-grep land in their own milestones
(M1, M5) under `infra/code_intel/` and are *called by* the engine — the
engine is the only thing `tool_code` sees.

## RetrievalCache (`cache.py`)

- Backed by the same SQLite DB `CodeContextEngine` already owns (`_default_db_path`).
- New table `retrieval_cache` with columns `(query_hash, tool_name, index_version, payload_json, hit_count, last_hit_at)`.
- Key: `sha256(canonical_json(args) + index_version + repo_id + tool_name)`.
- Index version bumps on `index_repo` and on M1's SCIP watcher invalidation.
- LRU eviction at 64 MB default; configurable.
- Returns `(hit: bool, payload: dict | None)`.

## BudgetPacker (`budget.py`)

- Reuses the tokenizer from `core/capabilities/repo_map/budget.py` (existing).
- `pack(items, budget_tokens, *, essential_keys, optional_keys_in_drop_order)`:
  1. Compute size with all fields.
  2. If over budget, drop optional keys in the documented order (see M12 for the frozen policy).
  3. Keep `essential_keys` for at least top-3 items.
  4. Return `(packed_items, dropped_count, token_count)`.

## Refactor existing `tool_code` ops to route through cache + budget

Targeted changes inside `engine.py` (and minor changes in
`mcp_server.py:tool_code` to surface `cache_hit` and `tokens_saved`):

| `tool_code` op | Change |
|---|---|
| `index` | Bump `index_version` on success. |
| `search` | Check cache first; on miss, call `search_symbols`, pack to `budget_tokens`, cache the packed result. Return payload with `cache_hit` and `tokens_saved`. |
| `symbol` | Cache single-symbol lookups by `(symbol_id\|qualified_name\|symbol_name)`. |
| `outline` | Cache file outlines by `(file_path, file_mtime)`. |
| `context` | Already accepts `budget_tokens`. Add cache by `(task_hash, seed_files, budget)`. |
| `impact` | Cache by `(file_path, file_mtime)`. |

Response shape gains two fields universally:
```json
{ ..., "cache_hit": false, "tokens_saved": 0, "provenance": "local" }
```

`provenance` becomes `"scip"` after M1, `"cached"` on hits, `"astgrep"` after M5, etc.

## Validation

Tests land in the existing gateway/core suites:

- `tests/core/test_code_context.py::test_retrieval_cache_hit_returns_cached_payload` — same args twice → second call returns `cache_hit=True` with the same packed search items.
- `tests/core/test_code_context.py::test_retrieval_cache_invalidated_on_index_bump` — `index_repo` increments `index_version`, forcing the next lookup to miss.
- `tests/core/test_code_context.py::test_budget_packer_drops_optional_keys_first` — fixture hit list of 10 → budget 200 tokens → top-3 keep essential keys.
- `tests/gateway/test_p0_mcp_surfaces.py::test_tool_code_search_returns_cache_hit_field` — MCP call surface includes `cache_hit`, `tokens_saved`, and `provenance`.
- `tests/core/test_code_context.py::test_provenance_local_default` — without M1, every result has `provenance="local"` or `provenance="cached"`.
- `tests/gateway/test_savings_api.py::test_record_context_budget_attaches_cache_metadata_for_code_tool` — telemetry events include `cache_hit`/`provenance` alongside `tokens_saved`.

## Exit criteria

- `RetrievalCache` and `BudgetPacker` exist and are unit-tested.
- All six existing `tool_code` ops route through both.
- `tool_code` response gains `cache_hit`, `tokens_saved`, `provenance` fields.
- Telemetry exports cache hit-rate alongside the existing
  `_record_smart_state_savings` path (see `mcp_server.py:574`).
- No new `@mcp_tool` registrations.

## Open questions

- **Cache eviction at LRU vs TTL.** Lean LRU + index-version invalidation; TTL only as a backstop.
- **Should `mcp__atelier__code op="cache_status"` be added now or in M12?** Lean M12 (the cache-hardening milestone).
- **Tokenizer choice.** Reuse `repo_map/budget.count_tokens`. Confirm it counts the same way as the model the agent uses; if not, plug in `tiktoken` for OpenAI-family compatibility.
