# Atelier V3 — Data Model Deltas

**Status:** Draft v2 · 2026-05-04
**Companion to:** [IMPLEMENTATION_PLAN_V3.md](IMPLEMENTATION_PLAN_V3.md),
[V2 data model](IMPLEMENTATION_PLAN_V2_DATA_MODEL.md).

---

## 0. Posture

V3 inherits the V2 schema verbatim. There are no new tables (other than an optional
`BenchmarkRun` for measured savings), no breaking column changes, and no breaking removals.

V3 changes are:

1. The `embedding` columns must be produced by a real `Embedder` — `stub_embedding` is gone.
2. A small provenance flag (`embedding_provenance`) is added so legacy rows can be reembedded.
3. A `BenchmarkRun` table is added so measured savings results have somewhere to live (replaces
   the hand-written YAML constants).

Everything else stays as V2 specified it.

---

## 1. Delta summary

| Concern                                                                                                               | V2                                      | V3                                                                                     |
| --------------------------------------------------------------------------------------------------------------------- | --------------------------------------- | -------------------------------------------------------------------------------------- |
| `MemoryBlock`, `ArchivalPassage`, `MemoryRecall`, `RunMemoryFrame`, `Trace`, `LessonCandidate`, `ReasonBlock` schemas | as V2                                   | **same as V2 — no field changes**                                                      |
| `archival_passage.embedding`, `lesson_candidate.embedding` columns                                                    | sometimes populated by `stub_embedding` | **must be from `Embedder.embed()`**; V3 fails closed if a SHA-hash artifact is written |
| New optional column `embedding_provenance` on `archival_passage` and `lesson_candidate`                               | n/a                                     | added; legacy rows flagged `legacy_stub`                                               |
| New `BenchmarkRun` table                                                                                              | n/a                                     | added in WP-50 to record measured savings results                                      |
| Deprecated: `infra/storage/vector.py::stub_embedding`                                                                 | present                                 | **deleted in WP-33**                                                                   |
| Deprecated: dual-write code paths in `infra/memory_bridges/letta_adapter.py`                                          | present                                 | **removed in WP-35**                                                                   |

There are no cross-runtime correlation IDs in V3 (the V3 draft v1 had `langgraph_session_id`,
`letta_agent_id`, `litellm_request_id` — those have been removed because Atelier does not run
LangGraph, does not call Letta as an executor, and does not call LiteLLM).

---

## 2. Embedding contract

### 2.1 Rule

Every column named `embedding` (or any equivalent vector field) must be produced by an
implementation of the `Embedder` Protocol from `src/atelier/infra/embeddings/base.py`. The
fallback for unavailable backends is `NullEmbedder`, which writes `[]`. It is **never**
`stub_embedding`.

### 2.2 Enforcement

- **Static.** A CI grep gate (`tests/infra/test_no_stub_embedding.py`) fails if any file under
  `src/atelier/` (excluding `tests/`) imports or calls `stub_embedding`.
- **Runtime.** `archival_passage.embedding`, `lesson_candidate.embedding`, and any future
  `*_embedding` column must satisfy:
  - empty (length 0), OR
  - dimension matches a registered embedder dimension (e.g. 384 for MiniLM, 1536 for
    `text-embedding-3-small`).
    32-byte SHA-hash artifacts (the V2 stub output) violate the dimension check and are
    rejected.

### 2.3 Migration of legacy rows

- A new column `embedding_provenance TEXT DEFAULT 'legacy_stub'` is added on
  `archival_passage` and `lesson_candidate`.
- New rows written via the `Embedder` protocol set this to the embedder class name
  (e.g. `'LocalEmbedder'`, `'OpenAIEmbedder'`).
- Until reembedding is complete, archival recall returns `legacy_stub` rows ranked by BM25
  only; the cosine score is forced to 0 and the recall result metadata surfaces the flag.
- A one-shot CLI `atelier reembed` (added in WP-47) reads legacy rows, calls the configured
  `Embedder`, writes new vectors, and clears the flag.

---

## 3. New: `BenchmarkRun` table

Replaces `benchmarks/swe/prompts_11.yaml` (hand-written constants) with a place to record
measurements.

### 3.1 Schema

```python
class BenchmarkRun(BaseModel):
    id: UUID
    started_at: datetime
    completed_at: datetime | None
    suite: str                  # "savings_replay_v3", future suites
    git_sha: str
    config_fingerprint: str     # SHA of the resolved .atelier/config.toml at run start

    # Per-prompt records live in benchmark_prompt_result table (FK to BenchmarkRun.id)

    # Aggregates, computed at completion
    n_prompts: int
    median_input_tokens_baseline: int | None     # Run A: ATELIER_DISABLE_ALL=1
    median_input_tokens_optimized: int | None    # Run B: defaults
    reduction_pct: float | None                  # (A - B) / A * 100
    notes: str | None
```

### 3.2 The 81 % claim

Until WP-50 publishes the first real `BenchmarkRun`:

- README and docs must either omit the percentage or qualify it as "design target" with a
  footnote linking to the open WP-50.
- `tests/docs/test_readme_no_unmeasured_claims.py` enforces this (gate added in WP-34).

---

## 4. Letta mapping

V3 keeps the V2 Atelier↔Letta type mapping:

- Atelier `MemoryBlock` ↔ Letta `Block` (`label`/`value`/`limit_chars` ↔ `label`/`value`/
  `limit`; `metadata` round-trips).
- Atelier `ArchivalPassage` ↔ Letta `Passage`.

V3 adds Atelier metadata under a stable `atelier_*` namespace inside Letta's `metadata` dict so
round-trip is collision-free:

- `atelier_session_id`
- `atelier_last_recall_at`
- `atelier_dedup_hash`

The mapping is implemented in `src/atelier/infra/memory_bridges/letta_adapter.py` and is
rewritten in WP-39 to be a single-primary path (no dual-write).

---

## 5. ReasonBlock schema — unchanged

V3 makes zero changes to the `ReasonBlock` Pydantic model. The store, retrieval, and review
surfaces are kept verbatim. ReasonBlocks are the part of Atelier with the highest correctness
requirements, and V3 introduces no churn on them.

---

## 6. Deprecation matrix

| Symbol                                                                                      | Status                                | Removed in | Replacement                                                                    |
| ------------------------------------------------------------------------------------------- | ------------------------------------- | ---------- | ------------------------------------------------------------------------------ |
| `infra/storage/vector.py::stub_embedding`                                                   | **deleted** in WP-33                  | V3.0       | `Embedder` protocol with a real backend                                        |
| `infra/memory_bridges/letta_adapter.py` dual-write paths                                    | **removed** in WP-35                  | V3.0       | single-primary backend, picked by `[memory].backend` config                    |
| `core/capabilities/context_compression/sleeptime.py` template implementation                | replaced or removed in WP-36          | V3.0       | per WP-36 decision (real LLM-call summarizer if Letta path; otherwise removed) |
| `benchmarks/swe/prompts_11.yaml` (hand-written savings constants)                           | retracted in WP-34; replaced in WP-50 | V3.0       | `BenchmarkRun` rows from real replay                                           |
| `core/capabilities/lesson_promotion/capability.py::_fingerprint` (string-prefix clustering) | replaced in WP-47                     | V3.0       | cosine clustering over real embeddings                                         |

Each row corresponds to a tracked WP. Each WP has acceptance tests proving the new path works
before the old one is removed.
