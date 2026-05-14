---
id: WP-08
title: MCP tools `memory_archive`, `memory_recall` + FTS+vector ranking
phase: B
pillar: 1, 3
owner_agent: atelier:code
depends_on: [WP-05, WP-06]
status: done
---

# WP-08 — Archival recall

## Why
Archival memory is the long-term knowledge surface. Pillar 3 depends on agents calling
`memory_recall(query)` instead of pasting prior context, so this packet must produce high-precision
top-k results with low latency.

## Files touched

- `src/atelier/gateway/adapters/mcp_server.py` — edit: register two more tools
- `src/atelier/gateway/adapters/cli.py` — edit: add `memory archive` and `memory recall` subcommands
- `src/atelier/core/capabilities/archival_recall/__init__.py` — new
- `src/atelier/core/capabilities/archival_recall/capability.py` — new
- `src/atelier/core/capabilities/archival_recall/ranking.py` — new
- `src/atelier/sdk/__init__.py` — edit
- `tests/core/test_archival_ranking.py`
- `tests/infra/test_archival_recall_recall_at_5.py`
- `tests/fixtures/archival_eval_questions.yaml` — 50 (query, expected_passage_id) pairs

## How to execute

1. Capability layout:

   ```python
   # core/capabilities/archival_recall/capability.py
   class ArchivalRecallCapability:
       def __init__(self, store: MemoryStore, embedder: Embedder, *, redactor): ...

       def archive(self, *, agent_id: str, text: str, source: str,
                   source_ref: str = "", tags: list[str] | None = None) -> ArchivalPassage: ...

       def recall(self, *, agent_id: str, query: str, top_k: int = 5,
                  tags: list[str] | None = None,
                  since: datetime | None = None) -> tuple[list[ArchivalPassage], MemoryRecall]: ...
   ```

2. `archive` flow:
   - redact text → embed (if Embedder is non-null) → store.insert_passage
   - if `dedup_hit`, return the existing row without re-embedding
   - chunk inputs > 800 chars into 400-char overlapping windows (`tiktoken`-based, not naive)

3. `recall` flow (hybrid scoring):
   - FTS5 candidate pool: top 50 by BM25
   - Vector candidate pool (only when embedder.dim > 0 and store has embeddings): top 50 by cosine
   - Merge and re-rank with `score = 0.6 * cosine + 0.4 * bm25_norm`
   - Apply tag filter, time filter
   - Cut to top_k
   - Persist a `MemoryRecall` row recording the query and top_passages

4. Ranking unit tests cover:
   - Pure FTS path (NullEmbedder selected)
   - Pure vector path (mocked embedder)
   - Hybrid path
   - Tag filter excludes mismatched passages
   - Time filter excludes old passages

5. Integration test loads `tests/fixtures/archival_eval_questions.yaml` (50 Q/A pairs over a
   100-passage seeded corpus) and asserts **recall@5 ≥ 0.8** with the local embedder. If the local
   embedder is not installed in CI, run with `NullEmbedder` and assert recall@5 ≥ 0.6 (FTS-only
   floor).

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/core/test_archival_ranking.py -v
LOCAL=1 uv run pytest tests/infra/test_archival_recall_recall_at_5.py -v

# CLI smoke
LOCAL=1 atelier memory archive --agent-id atelier:code --text "GIDs are stable, handles are not" --source user --tags shopify
LOCAL=1 atelier memory recall --agent-id atelier:code --query "shopify product identity" --top-k 3 --json

make verify
```

## Definition of done
- [ ] Hybrid ranking implemented; both pure-FTS and hybrid paths green-tested
- [ ] recall@5 ≥ 0.8 with local embedder, ≥ 0.6 FTS-only
- [ ] Recall row persisted on every recall call
- [ ] CLI smoke commands succeed
- [ ] `make verify` green
- [ ] `INDEX.md` updated; trace recorded
