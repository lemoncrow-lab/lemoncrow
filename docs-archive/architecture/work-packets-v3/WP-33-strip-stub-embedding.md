---
id: WP-33
title: Delete `stub_embedding`; route all callers through the `Embedder` protocol
phase: Z
boundary: Cleanup
owner_agent: atelier:code
depends_on: []
supersedes: []
status: done
---

# WP-33 — Strip `stub_embedding`

## Why

The 2026-05-04 audit found that `LessonPromoter`, the "hybrid" archival ranking, and at least
six other call sites bypass the `Embedder` protocol entirely and call
`infra/storage/vector.py::stub_embedding()` — a SHA-256 feature-hashing trick that returns a
32-byte deterministic vector. It is **not** a semantic embedding. Any clustering, recall, or
ranking that depends on it silently degrades to string-prefix matching with hash collisions.

This is a correctness bug, not a missing feature. Until it is removed, every claim downstream
("semantic recall", "lesson clustering precision", "hybrid ranking") is unverifiable. Phase H
integration packets cannot honestly measure improvement against a baseline that is broken.

**Phase Z is blocking on this packet.** All other V3 work waits.

## Files touched

- **DELETE:** `src/atelier/infra/storage/vector.py::stub_embedding` and any helper that
  exists only to support it.
- **EDIT, in `src/atelier/`:** every file that imports or calls `stub_embedding` — replace with
  the `Embedder` protocol obtained via `make_embedder()` from
  `src/atelier/infra/embeddings/factory.py`.
  - `src/atelier/core/capabilities/lesson_promotion/capability.py`
  - `src/atelier/core/capabilities/archival_recall/ranking.py` (or wherever the cosine path is)
  - any other caller surfaced by `grep -r stub_embedding src/atelier/`
- **EDIT:** `src/atelier/infra/embeddings/factory.py` — ensure `make_embedder()` returns
  `LocalEmbedder` by default when no env override is set, and that `LocalEmbedder` lazy-loads
  sentence-transformers on first call (no import cost at module load).
- **NEW:** `tests/infra/test_no_stub_embedding.py` — CI grep gate; fails if any file under
  `src/atelier/` (excluding `tests/`) imports or calls `stub_embedding`.
- **NEW:** `tests/core/test_embedder_routing.py` — every previously-broken call site
  (LessonPromoter, archival ranking, anything else found) is asserted to call `Embedder.embed()`
  exactly once with the expected text.
- **EDIT:** `src/atelier/infra/storage/sqlite_memory_store.py` — add a runtime
  `CHECK length(embedding) IN (0, 384*8, 1536*8, …)` on `archival_passage.embedding` (per
  data-model § 2.2). Or do equivalent Python-level guard if SQLite CHECK is impractical.
- **EDIT:** `src/atelier/infra/storage/sqlite_memory_store.py` — add `embedding_provenance`
  column on `archival_passage` and `lesson_candidate` (per data-model § 2.3).

## How to execute

1. **Inventory callers first** — do NOT start editing until you have run:

   ```bash
   grep -rn "stub_embedding" src/atelier/
   ```

   Record the full list in the PR description so reviewers can verify nothing is missed.

2. **Replace each caller in the smallest possible diff.** For each call site:
   - Inject an `Embedder` via constructor (preferred) or `make_embedder()` at the call site.
   - Replace `stub_embedding(text)` with `embedder.embed(text)` (note: protocol returns
     `list[float]`; check the existing signature).
   - If the caller currently can't fail (because `stub_embedding` is sync and infallible), wrap
     the new call to handle `EmbedderUnavailable` by falling back to `NullEmbedder` (which writes
     `[]`) and logging at WARNING. Do **not** crash the call site.

3. **Add the legacy provenance column** so existing rows remain readable:
   - Migration: `ALTER TABLE archival_passage ADD COLUMN embedding_provenance TEXT DEFAULT 'legacy_stub'`
   - Same on `lesson_candidate`.
   - New rows written via the Embedder protocol set this to `<embedder_class_name>` (e.g.
     `'LocalEmbedder'`).
   - Recall code: rows with `embedding_provenance='legacy_stub'` get cosine forced to 0 in
     hybrid ranking and a `legacy_stub: true` flag in the recall result metadata.

4. **Delete `stub_embedding`** only after the inventory is empty. Run the grep gate test
   locally first.

5. **Smoke test:** confirm `atelier-mcp` starts, `LessonPromoter.ingest_trace()` runs without
   `stub_embedding`, and archival recall returns rows (even if some are `legacy_stub`).

6. **Document the deferred re-embed** in the WP-47 packet (referenced from the data-model doc).
   This packet does not back-fill old rows — that is WP-47's job.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier

# 1. The CI grep gate passes (i.e., no more callers).
LOCAL=1 uv run pytest tests/infra/test_no_stub_embedding.py -v

# 2. The Embedder routing tests pass (every call site routes through the protocol).
LOCAL=1 uv run pytest tests/core/test_embedder_routing.py -v

# 3. The full repo's tests still pass with the new wiring.
make verify
```

## Definition of done

- [ ] `grep -rn stub_embedding src/atelier/` returns zero matches (excluding the deletion in the
      diff itself).
- [ ] `tests/infra/test_no_stub_embedding.py` passes (and is wired into `make verify`).
- [ ] `tests/core/test_embedder_routing.py` passes — explicit assertion per known caller.
- [ ] `archival_passage` and `lesson_candidate` carry `embedding_provenance`; legacy rows are
      flagged `legacy_stub`; new rows carry the embedder class name.
- [ ] Hybrid ranking forces cosine to 0 for `legacy_stub` rows; recall response surfaces the
      flag.
- [ ] `make verify` is green.
- [ ] V3 INDEX status updated to `done`. Trace recorded with `output_summary` containing
      "WP-33" and the count of call sites migrated.
