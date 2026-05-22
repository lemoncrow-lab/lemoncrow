# M6 — Function-level embeddings + hybrid RRF rank

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Layered inside `CodeContextEngine.search_symbols`, called from existing
> `tool_code op="search"`. **No new MCP tool.** Stub — flesh out on claim.

## Goal

Make `"find auth functions"` actually work. SCIP gives us a clean stream of
symbol units (name + signature + body + doc); we embed each unit with a code-
tuned model and rerank lexical hits via reciprocal rank fusion.

## Approach

- On every SCIP reindex, extract per-symbol units. Embed
  `name + signature + (doc or first 200 chars of body)`.
- Store vectors in a sidecar SQLite table `symbol_vectors`
  (same DB as `code_context` for locality).
- `SymbolIntelStore.find_symbol` adds `mode`:
  - `"lexical"` — current behavior (FTS).
  - `"semantic"` — vector cosine top-K.
  - `"hybrid"` — reciprocal rank fusion of lexical + semantic (default when `name` contains spaces).
- Auto-promote to `hybrid` when query looks like natural language (heuristic: contains spaces or stop words).

## Model choice (decide on claim)

| Candidate | Pros | Cons |
|---|---|---|
| `voyage-code-3` | Best-in-class for code; well-funded | API-only, $$$ at scale |
| `nomic-embed-code` | Open weights, runs locally | Smaller, less accurate |
| `jina-code-v2` | Open weights, fast | Newer, less proven |
| Reuse Atelier's current memory embedder | Zero new infra | Not code-tuned |

Recommendation: `nomic-embed-code` local + optional `voyage-code-3` API
fallback for power users. Gate via `.atelier/workspace.toml`.

## To flesh out on claim

- Embedding cache by `symbol_id` content hash (re-embed only on body change).
- RRF parameters (`k=60` typical).
- Benchmark: NDCG@5 on a curated query→symbol fixture set; target ≥ 0.7.
- Cost: embedding the Atelier repo's ~50k symbols with `nomic-embed-code` should fit in < 2 minutes on CPU.

## Exit criteria

- Hybrid mode default for multi-word queries.
- NDCG@5 ≥ 0.7 on fixture suite.
- Per-symbol embedding cost benchmark recorded.
- Validation matrix row added.
