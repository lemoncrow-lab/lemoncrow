---
id: WP-06
title: Implement SqliteMemoryStore + LettaMemoryStore adapter
phase: B
pillar: 1
owner_agent: atelier:code
depends_on: [WP-02, WP-03]
status: done
---

# WP-06 — Memory store

## Why
This is the heart of Pillar 1. Until this lands, the memory MCP tools and the dashboard memory page
have nothing to talk to.

## Files touched (all new unless marked)

- `src/atelier/infra/storage/memory_store.py` — `MemoryStore` Protocol
- `src/atelier/infra/storage/sqlite_memory_store.py` — `SqliteMemoryStore` impl
- `src/atelier/infra/memory_bridges/letta_adapter.py` — fill in stub from WP-03
- `src/atelier/infra/storage/factory.py` — edit: add `make_memory_store()`
- `tests/core/test_sqlite_memory_store.py`
- `tests/infra/test_memory_store_round_trip.py`
- `tests/infra/test_letta_adapter_fallback.py` (uses `respx` to mock sidecar)

## How to execute

1. Read [IMPLEMENTATION_PLAN_V2_DATA_MODEL.md § 7.1](../IMPLEMENTATION_PLAN_V2_DATA_MODEL.md#71-memorystore-new). Implement the
   Protocol exactly.

2. `SqliteMemoryStore`:
   - Uses the existing `sqlite_store.py` connection helper. Do **not** open a second DB file.
   - Every `upsert_block` writes a `MemoryBlockHistory` row in the same transaction.
   - Every `insert_passage` checks `(agent_id, dedup_hash)`; on collision returns the existing row
     and sets `dedup_hit=True` in the result.
   - `search_passages` uses pgvector when the embedding column is non-null AND `vector` extra is
     installed; otherwise falls back to FTS5 BM25 on `archival_passage_fts`.
   - All datetimes stored as ISO-8601 UTC strings (`datetime.isoformat()`).
   - Optimistic locking on `upsert_block`: refuse the write if the caller's `version` does not
     match the current row's `version`. Raise `MemoryConcurrencyError`.

3. `LettaMemoryStore`:
   - Implements the same Protocol.
   - Maps Atelier's `MemoryBlock` ↔ Letta `Block` (label/value/limit_chars → label/value/limit;
     metadata → metadata; pinned → custom Letta tag `atelier:pinned`).
   - For `search_passages`, calls the Letta archival search endpoint, then re-shapes the response.
   - On any sidecar exception, raise `MemorySidecarUnavailable` so the caller can fall back.

4. `make_memory_store(root, *, prefer="sqlite")`:
   - `prefer="letta"` and `LettaAdapter.is_available()` → `LettaMemoryStore`
   - Otherwise → `SqliteMemoryStore`
   - On `LettaMemoryStore` construction failure, log at WARNING and fall back to SQLite.

5. Tests:
   - Round-trip: upsert → get → list_pinned returns the row; history entry written
   - Optimistic locking: concurrent upsert with stale version raises
   - Dedup: insert same passage twice → second returns `dedup_hit=True`
   - Letta fallback: mock sidecar raises 503 → store transparently downgrades to SQLite

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/core/test_sqlite_memory_store.py \
                     tests/infra/test_memory_store_round_trip.py \
                     tests/infra/test_letta_adapter_fallback.py -v
make verify
```

## Definition of done
- [ ] `SqliteMemoryStore` and `LettaMemoryStore` both pass the same test suite via parametrization
- [ ] `MemoryConcurrencyError` raised on stale-version writes
- [ ] Letta path falls back to SQLite without losing data
- [ ] `make verify` green
- [ ] `INDEX.md` updated; trace recorded
