---
id: WP-35
title: Resolve `LettaMemoryStore` dual-write — pick one primary backend
phase: Z
boundary: Cleanup
owner_agent: atelier:code
depends_on: [WP-33]
supersedes: []
status: done
---

# WP-35 — Resolve LettaMemoryStore dual-write

## Why

The 2026-05-04 audit found that `infra/memory_bridges/letta_adapter.py` is a **dual-write
pass-through wrapper**, not a real Letta-backed store:

- Every `upsert_block` writes to Letta **and** SQLite.
- Reads prefer Letta with SQLite fallback.
- `insert_passage`, `record_recall`, run frames go to SQLite **only**.
- `search_passages` calls Letta archival but `_search_passage_rows` has no pgvector path —
  it does FTS5 with a regex-extracted OR-query.

The result: when Letta is configured, you pay double-write cost for marginal benefit, and the
"Letta-backed" archival is actually SQLite-FTS-backed. This blocks WP-39 ("Letta as primary")
because WP-39 cannot honestly migrate data while two stores diverge.

WP-35 is the _decision_ and _cleanup_. WP-39 is the _implementation_ of the chosen primary.

## Decision (made by this packet)

V3 picks **single-primary** with a config knob:

```toml
[memory]
backend = "letta"   # or "sqlite"
```

- `backend = "letta"`: Letta is read+write primary. SQLite is **not** written to. Reads on
  Letta-unavailable raise `MemorySidecarUnavailable` and the caller decides (typically the MCP
  surface returns an error rather than silently downgrading).
- `backend = "sqlite"`: SQLite is read+write primary. Letta is not consulted. This is the
  default for dev / unit tests / users without a Letta sidecar.

The dual-write path is removed. Period. Two stores of truth is the bug, not the feature.

## Files touched

- **EDIT:** `src/atelier/infra/memory_bridges/letta_adapter.py` — remove every `also_write_sqlite`
  / dual-write code path. The adapter becomes a _pure_ Letta client wrapper. If Letta is
  unavailable, raise `MemorySidecarUnavailable`.
- **EDIT:** `src/atelier/infra/storage/factory.py::make_memory_store()` — read
  `[memory].backend` from config (with env-var override `ATELIER_MEMORY_BACKEND`). Return
  exactly one store. Default `sqlite` if neither config nor env set.
- **EDIT:** `src/atelier/core/capabilities/...` — every caller that used to expect "writes go
  to both stores" needs to be checked. There should be none after this edit.
- **EDIT:** `src/atelier/infra/memory_bridges/letta_adapter.py` — implement a real archival
  search path that calls Letta's archival/recall endpoint. Stop reaching back into SQLite for
  passage rows.
- **NEW:** `tests/infra/test_letta_primary_no_sqlite_writes.py` — with `backend=letta` and a
  mocked Letta sidecar (via `respx`), assert that no rows are written to the SQLite memory
  tables during a full upsert/recall round-trip.
- **NEW:** `tests/infra/test_sqlite_primary_no_letta_calls.py` — with `backend=sqlite` and a
  Letta URL set in env, assert that no Letta HTTP calls are made.
- **EDIT:** `tests/infra/test_letta_adapter_fallback.py` — old V2 test asserted silent
  fallback. V3 asserts explicit error propagation: `MemorySidecarUnavailable` is raised, not
  swallowed.
- **EDIT:** `docs/architecture/IMPLEMENTATION_PLAN_V3_DATA_MODEL.md` — confirm § 7 (Letta
  mapping) is accurate after this packet's changes.
- **NEW:** `docs/internal/engineering/decisions/003-memory-single-primary.md` — short ADR
  recording the decision and trade-offs.

## How to execute

1. **Read `letta_adapter.py` end-to-end** before editing. The dual-write code is interleaved
   with the mapping code; you want to keep the mapping helpers (`map_block_to_letta`,
   `map_letta_to_block`) and remove only the dual-write branches.

2. **Add the config knob.** `make_memory_store()` should:
   - Read `ATELIER_MEMORY_BACKEND` env var first.
   - Fall back to `[memory].backend` in `.atelier/config.toml`.
   - Default to `sqlite`.
   - Log at INFO which backend was selected.

3. **Implement the real Letta archival search.** Use `letta-client`'s archival endpoint
   (check the V3 reference docstring or letta-client README). The cosine + BM25 hybrid math
   stays as a **policy** applied on the result list — the _retrieval_ itself is delegated to
   Letta. (This is consistent with the V3 boundary rule: we don't write a vector store; we
   apply ranking on top of one.)

4. **Remove dual-write.** Every `if self.also_write_sqlite: ...` branch goes. Every
   `record_recall` and `insert_passage` that bypassed Letta gets routed through Letta when
   `backend=letta`.

5. **Update tests.** The two new tests above prove the cleanup is real (no rogue writes /
   no rogue calls). The renamed `test_letta_adapter_fallback.py` proves errors are no longer
   silently swallowed.

6. **Write the ADR.** One page. Decision, alternatives considered (dual-write kept,
   read-from-Letta-write-to-both, this single-primary), why single-primary won.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier

LOCAL=1 uv run pytest tests/infra/test_letta_primary_no_sqlite_writes.py \
                     tests/infra/test_sqlite_primary_no_letta_calls.py \
                     tests/infra/test_letta_adapter_fallback.py -v

# Smoke: with no Letta env set, default backend is sqlite and works.
ATELIER_MEMORY_BACKEND= LOCAL=1 uv run python -c "from atelier.infra.storage.factory import make_memory_store; print(type(make_memory_store(None)).__name__)"
# Expect: SqliteMemoryStore

# Smoke: with backend=letta and no Letta running, error is explicit (not silent fallback).
ATELIER_MEMORY_BACKEND=letta LOCAL=1 uv run python -c "
from atelier.infra.storage.factory import make_memory_store
try:
    s = make_memory_store(None)
    s.list_pinned()
except Exception as e:
    print(type(e).__name__, str(e)[:80])
"
# Expect: MemorySidecarUnavailable

make verify
```

## Definition of done

- [ ] `letta_adapter.py` contains no dual-write code; every dual-write call site is removed.
- [ ] `make_memory_store()` selects exactly one backend based on config / env.
- [ ] `MemorySidecarUnavailable` raised explicitly when Letta is configured but unavailable;
      no silent fallback.
- [ ] Two new tests prove the no-rogue-writes / no-rogue-calls invariants.
- [ ] ADR `003-memory-single-primary.md` filed.
- [ ] V3 data model doc § 7 confirmed accurate.
- [ ] `make verify` green.
- [ ] V3 INDEX status updated. Trace recorded.
