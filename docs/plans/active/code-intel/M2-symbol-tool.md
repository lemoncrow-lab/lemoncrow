# M2 — Harden `code op="search"` (name-first lookup)

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Blocked by M0. Blocks M3, M7.

## Goal

`tool_code(op="search")` **already does** name-first symbol lookup
(`mcp_server.py:1942` → `engine.search_symbols(query, limit, kind, language)`).
This milestone is not "create a new symbol tool" — it's **harden the existing
op** so the response is outline-first, ranked, snippet-attached, budget-
packed, and labels its provenance. Once this lands, agents have a one-call
name → ranked SymbolHits flow without changing any MCP registration.

## What changes

### `tool_code` (mcp_server.py:1914) — extend the `op="search"` signature

Add parameters (additive only — existing callers keep working):

```python
op="search",
query: str,                                  # existing
limit: int = 20,                             # existing
kind: str | None = None,                     # existing
language: str | None = None,                 # existing
# new in M2:
snippet: Literal["none","head","full"] = "head",
snippet_lines: int = 8,
file_glob: str | None = None,                # passes through to engine
scope: Literal["repo","external","deleted"] = "repo",   # "external" wired by M9; "deleted" by M14; default "repo"
budget_tokens: int = 2000,
```

### `CodeContextEngine.search_symbols` — return enriched hits

- Accept the new params (`file_glob`, `scope`, `snippet`, `snippet_lines`).
- For each hit, optionally fetch a snippet via the existing `file_outline`
  byte-range read (cached by M0).
- Add a `provenance` field on each `SymbolRecord` (`"local"` until M1 lands;
  `"scip"` after; `"cached"` when M0's cache hits).

### Response shape

```json
{
  "items": [
    { "symbol_id": "...", "name": "...", "qualified_name": "...",
      "kind": "...", "file_path": "...", "start_line": 12, "end_line": 47,
      "signature": "def authenticate(user: User) -> Token", "score": 0.91,
      "snippet": "def authenticate(user: User) -> Token:\n    ...",
      "provenance": "local" },
    ...
  ],
  "truncated": false,
  "cache_hit": false,
  "tokens_saved": 0,
  "provenance_breakdown": { "local": 12, "cached": 8 }
}
```

`snippet="none"` is the cost-optimal default for navigation; `snippet="head"`
(8 lines) for "is this the right symbol?"; `snippet="full"` only when about
to edit.

## Implementation sketch

```python
# In engine.search_symbols(...)
def search_symbols(self, query, *, limit=20, kind=None, language=None,
                   file_glob=None, scope="repo", snippet="head",
                   snippet_lines=8, budget_tokens=2000):
    cache_key = self._cache.key("search", query, limit, kind, language,
                                file_glob, scope, snippet, snippet_lines)
    hit, payload = self._cache.get(cache_key)
    if hit:
        return payload | {"cache_hit": True}

    raw = self._raw_search_symbols(query, limit, kind, language, file_glob, scope)
    enriched = [self._attach_snippet(r, snippet, snippet_lines) for r in raw]
    packed, dropped, tokens = self._budget.pack(
        enriched, budget_tokens,
        essential_keys=("symbol_id","name","qualified_name","file_path","start_line"),
        optional_keys_in_drop_order=("snippet","doc_summary","signature"),
    )
    payload = {"items": packed, "truncated": dropped > 0, "cache_hit": False,
               "tokens_saved": 0, "provenance_breakdown": _breakdown(packed)}
    self._cache.put(cache_key, payload)
    return payload
```

## Validation

Tests under `tests/core/capabilities/code_context/test_search_hardened.py`:

- `test_search_exact_match` — exact name → one hit, correct file:line.
- `test_search_substring_returns_ranked` — substring → multiple hits, score-sorted.
- `test_kind_filter` — `kind="class"` excludes functions.
- `test_file_glob_narrows` — restricts to matching files.
- `test_snippet_none_no_body` — `snippet="none"` payload has no `snippet` field.
- `test_snippet_head_8_lines` — `snippet_lines=8` truncates correctly.
- `test_budget_truncates` — small `budget_tokens` → `truncated=True`, top-3 keep essentials.
- `test_cache_hit_on_repeat` — second identical call → `cache_hit=True`.
- `test_provenance_local_until_scip` — without M1, all hits are `provenance="local"`.

Token-cost benchmark `tests/benchmarks/code_intel/bench_search_vs_text_search.py`:

- 20-task suite where each task knows the symbol name.
- Baseline: `tool_smart_search` text search + `tool_smart_read`.
- New: `tool_code(op="search", snippet="head")`.
- Pass: new path uses ≤ 25% of baseline tokens averaged.

## Exit criteria

- New params on `tool_code(op="search")` work without breaking existing callers.
- Snippet rendering and provenance tagging implemented.
- Benchmark shows ≥ 75% token reduction vs `search + Read` baseline.
- Validation matrix updated with the hardened `code op="search"` row.

## Open questions

- **NL queries with spaces.** Auto-promote to hybrid lexical+semantic mode (which M6 introduces). Until M6 lands, multi-word queries degrade to substring match with a warning in the response payload.
- **Disambiguation.** When `query` matches multiple kinds/scopes, return all up to `limit`, ranked. Agent decides — cheaper than a round-trip.
- **Do we expose a `disambiguation_required` flag?** Lean yes — set `True` when top-2 scores are within 5%. Saves the agent a clarifying read.
