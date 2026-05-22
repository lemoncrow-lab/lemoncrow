# M12 — Outline-first audit + cache hardening + budget freeze

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Cross-cutting. Initial cut lives inside M0; this milestone hardens and
> validates discipline across every code-intel surface.

## Goal

This milestone is the *direct* cost reduction. The other milestones build
capability; this one enforces that every code-intel call uses it cheaply.

**Three mechanisms, each independently worth more than any single index:**

1. **Outline-first responses** — return signatures + 1-line summaries by
   default; bodies only on explicit ask. Already in place for `tool_smart_read`
   (mcp_server.py:1450); we extend the discipline across `code`, `edit`,
   `search`.
2. **Content-addressed retrieval cache** — same query twice = zero tokens,
   zero subprocess. Initial cut from M0; this milestone freezes the policy.
3. **Token budget enforcement** — every op takes `budget_tokens`; the engine
   packs the highest-information-density payload that fits. Initial cut from
   M0; this milestone freezes the packing order.

## Outline-first audit

Walk every code-intel-touching MCP entry. Confirm that the *default*
parameters return the cheapest useful payload:

| Tool / op | Audit target | Heavyweight mode (opt-in) |
|---|---|---|
| `code op="search"` (M2) | `snippet="head"` default | `snippet="full"` |
| `code op="usages"` (M3) | `group_by="file"` default | `group_by="caller"` |
| `code op="pattern"` (M5) | `limit=20`, match-only | `limit=100`, captures+snippets |
| `code op="callers"`/`"callees"` (M8) | `depth=1` | `depth>1` |
| `code op="recall"` / `memory op="recall_symbol"` (M7) | `include=["definition","memory"]` | `+["traces","decisions","tests"]` |
| `code op="outline"` (existing) | already outline-only | `+bodies=True` not needed; use `op="symbol"` |
| `read` (existing) | already outline-first; verify | — |
| `search` (existing) | already has `budget_tokens`; verify defaults | — |

No new top-level MCP tool. We may add `code op="cache_status"` (a
debug/diagnostic op) for humans/agents to inspect hit-rate, size, evictions.
Not part of the hot path.

## Retrieval cache hardening

Already scaffolded in M0 (`code_context/cache.py`); M12 sharpens the policy:

- Key shape frozen: `sha256(canonical_json(args) + index_version + repo_id + tool_name + op)`.
- Index version bumps on:
  - `tool_code op="index"` (existing).
  - SCIP reindex (M1 watcher).
  - ast-grep rewrite (M5).
  - Explicit `code op="cache_invalidate"` (new diagnostic op).
- TTL: indefinite within an index version.
- Hit returns instantly with `provenance="cached"`.
- Cache hit-rate exported alongside the existing `_record_smart_state_savings`
  telemetry path (mcp_server.py:574).

## Token-budget packer freeze

Already scaffolded in M0 (`code_context/budget.py`); M12 finalizes the
packing strategy as a documented contract:

**Packing order — drop in this sequence until payload fits:**

1. Drop `doc_summary` from hits ranked below median.
2. Drop `doc_summary` from all hits.
3. Drop snippets from hits ranked below top-5.
4. Drop snippets from all but top-3.
5. Drop signatures from anything past rank-N where N = `budget // 50 tokens`.
6. Drop entire hits past rank-N.

Top-3 always retain `symbol_id`, `name`, `file_path:start_line`, `signature`. If
even that doesn't fit, return an error — the caller asked for an impossible
budget.

Tokenizer: reuse `repo_map/budget.count_tokens` (existing). If the agent's
target model uses a different tokenizer (BPE vs cl100k_base etc.), add
`tiktoken` as an optional dep — decide on claim.

## Validation

**Cost-discipline benchmark** (`tests/benchmarks/code_intel/bench_cost_discipline.py`):

- 50-task suite covering navigation, refactor, recall, analysis.
- Each task records: tokens-in, tokens-out, cache hits, wall time.
- Suite passes iff aggregate token cost ≤ `0.30 × baseline` (baseline = pre-M0 implementation).

**Per-mechanism unit tests:**

- `test_default_mode_smaller_than_full` — same query, default payload ≤ 30% of `snippet="full"` payload.
- `test_cache_hit_zero_subprocess` — instrument SCIP/ast-grep adapters, second call → 0 subprocess invocations.
- `test_budget_packer_preserves_top3_essentials` — even at 100-token budget, top-3 keep id+name+file:line+signature.
- `test_provenance_cached_on_hit` — cache hit → `provenance="cached"` for every item.

## Exit criteria

- All ops from M2/M3/M4/M5/M7/M8 audited; defaults are outline-first.
- Cost-discipline benchmark passes (≤ 30% of baseline aggregate).
- Cache hit-rate metric visible in the Overview telemetry surface.
- `code op="cache_status"` and `code op="cache_invalidate"` diagnostic ops registered (no new top-level tool).
- Validation matrix updated.

## Open questions

- **Cache invalidation on memory writes.** Memory edits don't affect code intel, so they shouldn't bust the cache. Confirm with a test.
- **Cross-session warm.** Should we eagerly load SCIP readers at session start, or lazy on first query? Lean lazy.
- **Tokenizer mismatch.** If `repo_map/budget.count_tokens` is heuristic-only, decide whether to add `tiktoken` as a hard dep or leave as optional.
