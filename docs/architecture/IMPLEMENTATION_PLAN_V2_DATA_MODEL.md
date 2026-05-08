# Atelier V2 — Data Model

**Status:** Draft v1 · 2026-05-03
**Companion to:** [IMPLEMENTATION_PLAN_V2.md](IMPLEMENTATION_PLAN_V2.md)
**Audience:** Subagents implementing the schema-touching work-packets (WP-02, WP-06, WP-08, WP-15, WP-25..28).

This document defines every new Pydantic model V2 introduces, every new SQLite/PostgreSQL table,
and every adapter to the existing `ReasonBlock` / `Trace` models in
`src/atelier/core/foundation/models.py`. **Subagents must not invent fields outside this document.**
If a packet needs a new field, raise it as a comment and stop.

---

## 1. Existing models (untouched)

| Model            | File                        | Notes                                  |
| ---------------- | --------------------------- | -------------------------------------- |
| `ReasonBlock`    | `core/foundation/models.py` | Stays the canonical procedure schema   |
| `Trace`          | `core/foundation/models.py` | Stays the canonical observation schema |
| `RawArtifact`    | `core/foundation/models.py` | Unchanged                              |
| `Rubric`         | `core/foundation/models.py` | Unchanged                              |
| `Environment`    | `core/foundation/models.py` | Unchanged                              |
| `LedgerEvent`    | `core/foundation/models.py` | Unchanged                              |
| `FailureCluster` | `core/foundation/models.py` | Unchanged                              |
| `EvalCase`       | `core/foundation/models.py` | Unchanged                              |

V2 **never** edits these. New fields go on new models, joined by foreign-keys.

---

## 2. New models — Memory subsystem

All new memory models live in `src/atelier/core/foundation/memory_models.py` (NEW). All of them are
`pydantic.BaseModel` subclasses with `model_config = ConfigDict(extra="forbid")`.

### 2.1 `MemoryBlock`

A Letta-derived editable core-memory block. Lives in the prompt or is recalled into it.

| Field                | Type             | Required | Default          | Notes                                                             |
| -------------------- | ---------------- | -------- | ---------------- | ----------------------------------------------------------------- |
| `id`                 | `str`            | yes      | —                | Format: `mem-<uuid7>`                                             |
| `agent_id`           | `str`            | yes      | —                | Logical agent name (e.g. `atelier:code`, `legacy.external-agent`) |
| `label`              | `str`            | yes      | —                | Human-readable; unique per `(agent_id, label)`                    |
| `value`              | `str`            | yes      | —                | Body text                                                         |
| `limit_chars`        | `int`            | no       | `8000`           | Hard cap; writes that exceed it are rejected                      |
| `description`        | `str`            | no       | `""`             | Why this block exists                                             |
| `read_only`          | `bool`           | no       | `false`          | If `true`, agent tools cannot mutate                              |
| `metadata`           | `dict[str, Any]` | no       | `\&#123;\&#125;` | Arbitrary JSON                                                    |
| `pinned`             | `bool`           | no       | `false`          | If `true`, included in every prompt; ignored by recall scoring    |
| `version`            | `int`            | no       | `1`              | Optimistic locking; incremented on each update                    |
| `current_history_id` | `str \| None`    | no       | `null`           | FK to `MemoryBlockHistory.id`                                     |
| `created_at`         | `datetime`       | no       | `_utcnow()`      | UTC                                                               |
| `updated_at`         | `datetime`       | no       | `_utcnow()`      | UTC                                                               |

**Indexes:**

- `unique(agent_id, label)` — primary recall key
- `(agent_id, pinned)` — fast pinned-block fetch on every turn
- `(updated_at)` — recency scoring

### 2.2 `MemoryBlockHistory`

Append-only audit trail. Created on every `update_block` call.

| Field        | Type       | Required | Notes                                  |
| ------------ | ---------- | -------- | -------------------------------------- |
| `id`         | `str`      | yes      | `memh-<uuid7>`                         |
| `block_id`   | `str`      | yes      | FK `MemoryBlock.id`                    |
| `prev_value` | `str`      | yes      | Value before the update                |
| `new_value`  | `str`      | yes      | Value after the update                 |
| `actor`      | `str`      | yes      | `agent:atelier:code` or `human:pankaj` |
| `reason`     | `str`      | no       | Free-text                              |
| `created_at` | `datetime` | no       | UTC                                    |

**Indexes:** `(block_id, created_at)` — chronological diff view in the dashboard.

### 2.3 `ArchivalPassage`

Long-term memory chunk recalled by semantic search.

| Field             | Type                  | Required | Notes                                                              |
| ----------------- | --------------------- | -------- | ------------------------------------------------------------------ |
| `id`              | `str`                 | yes      | `pas-<uuid7>`                                                      |
| `agent_id`        | `str`                 | yes      | Same as MemoryBlock                                                |
| `text`            | `str`                 | yes      | The chunk                                                          |
| `embedding`       | `list[float] \| None` | no       | 384-d (local) or 1536-d (OpenAI). `null` until the embedder runs.  |
| `embedding_model` | `str`                 | no       | E.g. `local:all-MiniLM-L6-v2`                                      |
| `tags`            | `list[str]`           | no       | Free-tag list — used for filter-then-rank queries                  |
| `source`          | `str`                 | yes      | One of `trace`, `block_evict`, `user`, `tool_output`, `file_chunk` |
| `source_ref`      | `str`                 | no       | `trace:<id>`, `block:<id>`, `file:<path>:<sha>`                    |
| `dedup_hash`      | `str`                 | yes      | `blake3(text)` — used to skip near-dup writes                      |
| `created_at`      | `datetime`            | no       | UTC                                                                |

**Indexes:**

- `(agent_id, created_at desc)` — time-bounded scans
- `unique(agent_id, dedup_hash)` — dedup
- `(source, source_ref)` — fast back-link to the originating trace/block
- pgvector index on `embedding` when `vector` extra installed

### 2.4 `MemoryRecall`

A single retrieval event, used for recall@k metric and dashboard.

| Field                 | Type          | Required | Notes                                |
| --------------------- | ------------- | -------- | ------------------------------------ |
| `id`                  | `str`         | yes      | `rec-<uuid7>`                        |
| `agent_id`            | `str`         | yes      |                                      |
| `query`               | `str`         | yes      | Redacted free text                   |
| `top_passages`        | `list[str]`   | yes      | Ordered list of `ArchivalPassage.id` |
| `selected_passage_id` | `str \| None` | no       | Which one the agent quoted, if any   |
| `created_at`          | `datetime`    | no       | UTC                                  |

### 2.5 `RunMemoryFrame`

The per-run snapshot of which memory blocks were active and which passages were recalled. Lets us
prove memory worked when reviewing a trace.

| Field                 | Type        | Required | Notes                                |
| --------------------- | ----------- | -------- | ------------------------------------ |
| `run_id`              | `str`       | yes      | FK to existing run ledger            |
| `pinned_blocks`       | `list[str]` | yes      | Block IDs in the system prompt       |
| `recalled_passages`   | `list[str]` | yes      | Passage IDs returned to agent        |
| `summarized_events`   | `list[str]` | yes      | Ledger event IDs that were compacted |
| `tokens_pre_summary`  | `int`       | yes      | From the context-window calculator   |
| `tokens_post_summary` | `int`       | yes      | After sleeptime ran                  |
| `compaction_strategy` | `str`       | yes      | `none`, `tfidf`, `letta_summarizer`  |
| `created_at`          | `datetime`  | no       | UTC                                  |

---

## 3. New models — Lesson pipeline

Lives in `src/atelier/core/foundation/lesson_models.py` (NEW).

### 3.1 `LessonCandidate`

A draft ReasonBlock-or-rubric-edit waiting for human review.

| Field                   | Type                                                   | Required | Notes                                                    |
| ----------------------- | ------------------------------------------------------ | -------- | -------------------------------------------------------- |
| `id`                    | `str`                                                  | yes      | `lc-<uuid7>`                                             |
| `domain`                | `str`                                                  | yes      | E.g. `state.change`                                      |
| `cluster_fingerprint`   | `str`                                                  | yes      | Same as `FailureCluster.fingerprint` if derived from one |
| `kind`                  | `Literal["new_block","edit_block","new_rubric_check"]` | yes      |                                                          |
| `target_id`             | `str \| None`                                          | no       | Existing block/rubric ID if `edit_*`                     |
| `proposed_block`        | `ReasonBlock \| None`                                  | no       | Full draft if `kind="new_block"`                         |
| `proposed_rubric_check` | `str \| None`                                          | no       | Single check name if `new_rubric_check`                  |
| `evidence_trace_ids`    | `list[str]`                                            | yes      | Traces backing the proposal                              |
| `embedding`             | `list[float] \| None`                                  | no       |                                                          |
| `confidence`            | `float`                                                | yes      | 0..1; informs UI sort order                              |
| `status`                | `Literal["inbox","approved","rejected","superseded"]`  | no       | default `inbox`                                          |
| `reviewer`              | `str \| None`                                          | no       | `human:pankaj` once decided                              |
| `decision_at`           | `datetime \| None`                                     | no       |                                                          |
| `decision_reason`       | `str`                                                  | no       |                                                          |
| `created_at`            | `datetime`                                             | no       | UTC                                                      |

**Indexes:** `(domain, status, created_at desc)` — dashboard inbox query.

### 3.2 `LessonPromotion`

Audit row written when a `LessonCandidate` is approved → ReasonBlock published.

| Field                | Type          | Required | Notes                         |
| -------------------- | ------------- | -------- | ----------------------------- |
| `id`                 | `str`         | yes      | `lp-<uuid7>`                  |
| `lesson_id`          | `str`         | yes      | FK `LessonCandidate.id`       |
| `published_block_id` | `str \| None` | no       | FK `ReasonBlock.id` (if new)  |
| `edited_block_id`    | `str \| None` | no       | FK `ReasonBlock.id` (if edit) |
| `pr_url`             | `str`         | no       | Optional GitHub PR URL        |
| `created_at`         | `datetime`    | no       | UTC                           |

---

## 4. New models — Context-savings instrumentation

Lives in `src/atelier/core/foundation/savings_models.py` (NEW). Used only for telemetry; never
injected into agent prompts.

### 4.1 `ContextBudget`

Per-turn snapshot, written by the MCP gateway after every tool call.

| Field                | Type             | Required | Notes                                                                            |
| -------------------- | ---------------- | -------- | -------------------------------------------------------------------------------- |
| `id`                 | `str`            | yes      | `cb-<uuid7>`                                                                     |
| `run_id`             | `str`            | yes      | FK to run ledger                                                                 |
| `turn_index`         | `int`            | yes      | 0-based                                                                          |
| `model`              | `str`            | yes      | E.g. `claude-opus-4.7`                                                           |
| `input_tokens`       | `int`            | yes      | Prompt sent                                                                      |
| `cache_read_tokens`  | `int`            | yes      | From provider response                                                           |
| `cache_write_tokens` | `int`            | yes      |                                                                                  |
| `output_tokens`      | `int`            | yes      |                                                                                  |
| `naive_input_tokens` | `int`            | yes      | What the prompt would have been **without** Atelier — required for savings claim |
| `lever_savings`      | `dict[str, int]` | yes      | E.g. `\&#123;"reasonblock_inject": 420, "search_read": 1850, …\&#125;`           |
| `tool_calls`         | `int`            | yes      | Per-turn                                                                         |
| `created_at`         | `datetime`       | no       | UTC                                                                              |

The `naive_input_tokens` calculation is the heart of the >50 % claim. It is computed by replaying
the same agent loop with all Atelier capabilities disabled (gated by `ATELIER_DISABLE_ALL=1`) and
recorded once per benchmark run, not per production call. WP-19 owns the harness.

---

## 5. New models — Routing and verification

Lives in `src/atelier/core/foundation/routing_models.py` (NEW). Used by Phase F and by evals. These
models store only observable policy decisions and validation outcomes. They never store hidden
chain-of-thought.

### 5.1 `AgentRequest`

Normalized host request used by the router.

| Field           | Type                                                                           | Required | Default | Notes                                    |
| --------------- | ------------------------------------------------------------------------------ | -------- | ------- | ---------------------------------------- |
| `id`            | `str`                                                                          | yes      | —       | `req-<uuid7>`                            |
| `run_id`        | `str \| None`                                                                  | no       | `null`  | Existing run ledger ID if known          |
| `user_goal`     | `str`                                                                          | yes      | —       | Redacted user-facing goal                |
| `repo_root`     | `str`                                                                          | yes      | —       | Absolute or workspace-relative repo root |
| `task_type`     | `Literal["debug","feature","refactor","test","explain","review","docs","ops"]` | yes      | —       | Router task class                        |
| `risk_level`    | `Literal["low","medium","high"]`                                               | yes      | —       | Pre-router risk estimate                 |
| `changed_files` | `list[str]`                                                                    | no       | `[]`    | Current git/diff scope                   |
| `created_at`    | `datetime`                                                                     | no       | UTC now |                                          |

### 5.2 `ContextBudgetPolicy`

Routing budget constraints. Model/provider names are configuration data, not policy constants.

| Field                 | Type                                          | Required | Default        | Notes                                   |
| --------------------- | --------------------------------------------- | -------- | -------------- | --------------------------------------- |
| `max_input_tokens`    | `int`                                         | yes      | —              | Hard prompt budget for the step         |
| `premium_call_budget` | `int`                                         | no       | `1`            | Max premium calls allowed before review |
| `cache_policy`        | `Literal["prefer_cache","neutral","disable"]` | no       | `prefer_cache` | Provider-neutral cache intent           |
| `cheap_model`         | `str`                                         | no       | `""`           | Loaded from config                      |
| `mid_model`           | `str`                                         | no       | `""`           | Loaded from config                      |
| `premium_model`       | `str`                                         | no       | `""`           | Loaded from config                      |

### 5.3 `RouteDecision`

One route decision for one agent step.

| Field                  | Type                                                                                                    | Required | Default | Notes                                      |
| ---------------------- | ------------------------------------------------------------------------------------------------------- | -------- | ------- | ------------------------------------------ |
| `id`                   | `str`                                                                                                   | yes      | —       | `rd-<uuid7>`                               |
| `run_id`               | `str`                                                                                                   | yes      | —       | Run ledger ID                              |
| `request_id`           | `str`                                                                                                   | no       | `""`    | `AgentRequest.id` if stored                |
| `step_index`           | `int`                                                                                                   | yes      | —       | 0-based per run                            |
| `step_type`            | `Literal["classify","compress","retrieve","plan","edit","debug","verify","summarize","lesson_extract"]` | yes      | —       |                                            |
| `risk_level`           | `Literal["low","medium","high"]`                                                                        | yes      | —       | Risk at decision time                      |
| `tier`                 | `Literal["deterministic","cheap","mid","premium"]`                                                      | yes      | —       | Selected execution tier                    |
| `selected_model`       | `str`                                                                                                   | no       | `""`    | Empty for deterministic/tool-only path     |
| `confidence`           | `float`                                                                                                 | yes      | —       | 0..1                                       |
| `reason`               | `str`                                                                                                   | yes      | —       | Short observable explanation               |
| `protected_file_match` | `bool`                                                                                                  | no       | `false` | True when file policy elevated risk        |
| `verifier_required`    | `list[str]`                                                                                             | no       | `[]`    | e.g. `["pytest","ruff","rubric"]`          |
| `escalation_trigger`   | `str \| None`                                                                                           | no       | `null`  | Set when decision escalates                |
| `evidence_refs`        | `list[str]`                                                                                             | no       | `[]`    | Trace, file, test, or ReasonBlock pointers |
| `created_at`           | `datetime`                                                                                              | no       | UTC now |                                            |

### 5.4 `VerificationEnvelope`

Observed verifier outcome for a route decision. It consumes validation results; it does not execute
commands itself.

| Field                 | Type                                       | Required | Default   | Notes                                   |
| --------------------- | ------------------------------------------ | -------- | --------- | --------------------------------------- |
| `id`                  | `str`                                      | yes      | —         | `ve-<uuid7>`                            |
| `route_decision_id`   | `str`                                      | yes      | —         | FK to `RouteDecision.id`                |
| `run_id`              | `str`                                      | yes      | —         | Run ledger ID                           |
| `changed_files`       | `list[str]`                                | no       | `[]`      | Observed diff scope                     |
| `validation_results`  | `list[ValidationResult]`                   | no       | `[]`      | Existing model from `foundation.models` |
| `rubric_status`       | `Literal["not_run","pass","warn","fail"]`  | no       | `not_run` |                                         |
| `outcome`             | `Literal["pass","warn","fail","escalate"]` | yes      | —         | Verifier result                         |
| `compressed_evidence` | `str`                                      | no       | `""`      | Short evidence for retry/escalation     |
| `human_accepted`      | `bool \| None`                             | no       | `null`    | Optional post-run signal                |
| `created_at`          | `datetime`                                 | no       | UTC now   |                                         |

### 5.5 `RoutingEvalSummary`

Computed report object; persisted only when a benchmark run asks for a saved report.

| Field                     | Type       | Required | Notes                                  |
| ------------------------- | ---------- | -------- | -------------------------------------- |
| `run_id`                  | `str`      | yes      | Benchmark or production run ID         |
| `cost_per_accepted_patch` | `float`    | yes      | Total cost / accepted patch count      |
| `premium_call_rate`       | `float`    | yes      | Premium steps / routed steps           |
| `cheap_success_rate`      | `float`    | yes      | Verified cheap successes / cheap tries |
| `escalation_success_rate` | `float`    | yes      | Accepted escalations / escalations     |
| `routing_regression_rate` | `float`    | yes      | Regressed outcomes / accepted outputs  |
| `created_at`              | `datetime` | no       | UTC                                    |

---

## 6. SQLite / Postgres schema

DDL goes in `src/atelier/infra/storage/migrations/v2_*.sql` (NEW). Apply via the existing
`StorageBackend.migrate()` mechanism.

### 6.1 New tables

```sql
-- v2_001_memory.sql
CREATE TABLE memory_block (
  id                  TEXT PRIMARY KEY,
  agent_id            TEXT NOT NULL,
  label               TEXT NOT NULL,
  value               TEXT NOT NULL,
  limit_chars         INTEGER NOT NULL DEFAULT 8000,
  description         TEXT NOT NULL DEFAULT '',
  read_only           INTEGER NOT NULL DEFAULT 0,
  metadata            TEXT NOT NULL DEFAULT '&#123;&#125;',
  pinned              INTEGER NOT NULL DEFAULT 0,
  version             INTEGER NOT NULL DEFAULT 1,
  current_history_id  TEXT,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  UNIQUE (agent_id, label)
);
CREATE INDEX ix_memory_block_agent_pinned ON memory_block(agent_id, pinned);
CREATE INDEX ix_memory_block_updated_at  ON memory_block(updated_at DESC);

CREATE TABLE memory_block_history (
  id          TEXT PRIMARY KEY,
  block_id    TEXT NOT NULL REFERENCES memory_block(id) ON DELETE CASCADE,
  prev_value  TEXT NOT NULL,
  new_value   TEXT NOT NULL,
  actor       TEXT NOT NULL,
  reason      TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);
CREATE INDEX ix_memory_block_history_block_at
  ON memory_block_history(block_id, created_at DESC);

CREATE TABLE archival_passage (
  id              TEXT PRIMARY KEY,
  agent_id        TEXT NOT NULL,
  text            TEXT NOT NULL,
  embedding       BLOB,
  embedding_model TEXT NOT NULL DEFAULT '',
  tags            TEXT NOT NULL DEFAULT '[]',
  source          TEXT NOT NULL,
  source_ref      TEXT NOT NULL DEFAULT '',
  dedup_hash      TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  UNIQUE (agent_id, dedup_hash)
);
CREATE INDEX ix_archival_passage_agent_at ON archival_passage(agent_id, created_at DESC);
CREATE INDEX ix_archival_passage_source   ON archival_passage(source, source_ref);
-- FTS5 mirror for non-vector search:
CREATE VIRTUAL TABLE archival_passage_fts USING fts5(
  text, tags, content='archival_passage', content_rowid='rowid'
);

CREATE TABLE memory_recall (
  id                   TEXT PRIMARY KEY,
  agent_id             TEXT NOT NULL,
  query                TEXT NOT NULL,
  top_passages         TEXT NOT NULL,
  selected_passage_id  TEXT,
  created_at           TEXT NOT NULL
);

CREATE TABLE run_memory_frame (
  run_id              TEXT PRIMARY KEY,
  pinned_blocks       TEXT NOT NULL,
  recalled_passages   TEXT NOT NULL,
  summarized_events   TEXT NOT NULL,
  tokens_pre_summary  INTEGER NOT NULL,
  tokens_post_summary INTEGER NOT NULL,
  compaction_strategy TEXT NOT NULL,
  created_at          TEXT NOT NULL
);
```

```sql
-- v2_002_lessons.sql
CREATE TABLE lesson_candidate (
  id                     TEXT PRIMARY KEY,
  domain                 TEXT NOT NULL,
  cluster_fingerprint    TEXT NOT NULL DEFAULT '',
  kind                   TEXT NOT NULL,
  target_id              TEXT,
  proposed_block_json    TEXT,
  proposed_rubric_check  TEXT,
  evidence_trace_ids     TEXT NOT NULL,
  embedding              BLOB,
  confidence             REAL NOT NULL,
  status                 TEXT NOT NULL DEFAULT 'inbox',
  reviewer               TEXT,
  decision_at            TEXT,
  decision_reason        TEXT NOT NULL DEFAULT '',
  created_at             TEXT NOT NULL
);
CREATE INDEX ix_lesson_candidate_domain_status_at
  ON lesson_candidate(domain, status, created_at DESC);

CREATE TABLE lesson_promotion (
  id                  TEXT PRIMARY KEY,
  lesson_id           TEXT NOT NULL REFERENCES lesson_candidate(id),
  published_block_id  TEXT,
  edited_block_id     TEXT,
  pr_url              TEXT NOT NULL DEFAULT '',
  created_at          TEXT NOT NULL
);
```

```sql
-- v2_003_context_budget.sql
CREATE TABLE context_budget (
  id                   TEXT PRIMARY KEY,
  run_id               TEXT NOT NULL,
  turn_index           INTEGER NOT NULL,
  model                TEXT NOT NULL,
  input_tokens         INTEGER NOT NULL,
  cache_read_tokens    INTEGER NOT NULL,
  cache_write_tokens   INTEGER NOT NULL,
  output_tokens        INTEGER NOT NULL,
  naive_input_tokens   INTEGER NOT NULL,
  lever_savings_json   TEXT NOT NULL,
  tool_calls           INTEGER NOT NULL,
  created_at           TEXT NOT NULL,
  UNIQUE (run_id, turn_index)
);
CREATE INDEX ix_context_budget_run ON context_budget(run_id);
```

```sql
-- v2_004_routing.sql
CREATE TABLE route_decision (
  id                    TEXT PRIMARY KEY,
  run_id                TEXT NOT NULL,
  request_id            TEXT NOT NULL DEFAULT '',
  step_index            INTEGER NOT NULL,
  step_type             TEXT NOT NULL,
  risk_level            TEXT NOT NULL,
  tier                  TEXT NOT NULL,
  selected_model        TEXT NOT NULL DEFAULT '',
  confidence            REAL NOT NULL,
  reason                TEXT NOT NULL,
  protected_file_match  INTEGER NOT NULL DEFAULT 0,
  verifier_required     TEXT NOT NULL DEFAULT '[]',
  escalation_trigger    TEXT,
  evidence_refs         TEXT NOT NULL DEFAULT '[]',
  created_at            TEXT NOT NULL
);
CREATE INDEX ix_route_decision_run_step ON route_decision(run_id, step_index);

CREATE TABLE verification_envelope (
  id                    TEXT PRIMARY KEY,
  route_decision_id     TEXT NOT NULL REFERENCES route_decision(id) ON DELETE CASCADE,
  run_id                TEXT NOT NULL,
  changed_files         TEXT NOT NULL DEFAULT '[]',
  validation_results    TEXT NOT NULL DEFAULT '[]',
  rubric_status         TEXT NOT NULL DEFAULT 'not_run',
  outcome               TEXT NOT NULL,
  compressed_evidence   TEXT NOT NULL DEFAULT '',
  human_accepted        INTEGER,
  created_at            TEXT NOT NULL
);
CREATE INDEX ix_verification_envelope_route ON verification_envelope(route_decision_id);
CREATE INDEX ix_verification_envelope_run ON verification_envelope(run_id);
```

### 6.2 PostgreSQL deltas

Mirror SQLite verbatim except:

- `BLOB` → `BYTEA`
- Add `embedding vector(384)` column under `pgvector` when `vector` extra is installed (alembic guard).
- Add `CREATE INDEX … USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)`.

The Postgres-only `vector` index is created lazily by
`infra/storage/postgres_store.py:ensure_vector_index()`; absent the extension the runtime falls
back to FTS5 + cosine in Python on a candidate set ≤ 200.

---

## 7. Adapter contracts

### 7.1 `MemoryStore` (NEW)

```python
# src/atelier/infra/storage/memory_store.py
class MemoryStore(Protocol):
    def upsert_block(self, block: MemoryBlock, *, actor: str, reason: str = "") -> MemoryBlock: ...
    def get_block(self, agent_id: str, label: str) -> MemoryBlock | None: ...
    def list_pinned_blocks(self, agent_id: str) -> list[MemoryBlock]: ...
    def list_block_history(self, block_id: str, *, limit: int = 50) -> list[MemoryBlockHistory]: ...
    def delete_block(self, block_id: str) -> None: ...

    def insert_passage(self, passage: ArchivalPassage) -> ArchivalPassage: ...
    def search_passages(
        self, agent_id: str, query: str, *, top_k: int = 5,
        tags: list[str] | None = None, since: datetime | None = None
    ) -> list[ArchivalPassage]: ...

    def write_run_frame(self, frame: RunMemoryFrame) -> None: ...
    def get_run_frame(self, run_id: str) -> RunMemoryFrame | None: ...
```

Two implementations:

- `SqliteMemoryStore` (default) — `infra/storage/sqlite_memory_store.py`
- `LettaMemoryStore` (opt-in) — `infra/memory_bridges/letta_adapter.py`; falls through to Sqlite for fields Letta doesn't expose

### 7.2 `Embedder` (NEW)

```python
# src/atelier/infra/embeddings/base.py
class Embedder(Protocol):
    dim: int
    name: str
    def embed(self, texts: list[str]) -> list[list[float]]: ...
```

Implementations: `LocalEmbedder`, `OpenAIEmbedder`, `LettaEmbedder`, `NullEmbedder` (returns `[]`,
forces FTS-only fallback). Selection logic in `infra/embeddings/factory.py`.

### 7.3 `LessonPromoter` (NEW)

```python
# src/atelier/core/capabilities/lesson_promotion/capability.py
class LessonPromoter:
    def ingest_trace(self, trace: Trace) -> LessonCandidate | None: ...
    def list_inbox(self, *, domain: str | None = None, limit: int = 50) -> list[LessonCandidate]: ...
    def approve(self, lesson_id: str, *, reviewer: str, reason: str = "") -> LessonPromotion: ...
    def reject(self, lesson_id: str, *, reviewer: str, reason: str) -> None: ...
```

---

## 8. New MCP tools

Added to `src/atelier/gateway/adapters/mcp_server.py`. All names prefixed `atelier_` for namespace
hygiene.

| MCP tool                | Pillar  | Owner WP | Input                                                                 | Output                                                                                                                |
| ----------------------- | ------- | -------- | --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `memory`                | 1       | WP-07    | `agent_id, label, value, [pinned, read_only, description]`            | `\&#123; id, version \&#125;`                                                                                         |
| `memory`                | 1       | WP-07    | `agent_id, label`                                                     | `MemoryBlock`                                                                                                         |
| `memory`                | 1, 3    | WP-08    | `agent_id, query, [top_k, tags]`                                      | `\&#123; passages: [\&#123;id, text, score, source_ref\&#125;], recall_id \&#125;`                                    |
| `memory`                | 1       | WP-08    | `agent_id, text, source, [source_ref, tags]`                          | `\&#123; id, dedup_hit \&#125;`                                                                                       |
| `memory`                | 1, 3    | WP-09    | `run_id`                                                              | `\&#123; tokens_pre, tokens_post, summary_md, evicted_event_ids \&#125;`                                              |
| `atelier lesson inbox`  | 2       | WP-15    | `[domain, limit]`                                                     | `[LessonCandidate]`                                                                                                   |
| `atelier lesson decide` | 2       | WP-15    | `lesson_id, decision: "approve"                                       | "reject", reviewer, reason`                                                                                           | `\&#123; status, promotion_id? \&#125;`                                                    |
| `search`                | 3       | WP-21    | `query, [path, max_files, max_chars_per_file]`                        | `\&#123; matches: [\&#123;path, line_start, line_end, snippet, lang_outline?\&#125;], total_chars, cache_hit \&#125;` |
| `edit`                  | 3       | WP-22    | `edits: [\&#123;path, old_string                                      | range, new_string, fuzzy?: bool\&#125;]`                                                                              | `\&#123; applied: [\&#123;path, hunk\&#125;], failed: [\&#123;path, error\&#125;] \&#125;` |
| `atelier sql inspect`   | 3       | WP-23    | `connection_alias, sql`                                               | `\&#123; rows: [...], columns: [...], affected: int, truncated: bool \&#125;`                                         |
| `compact`               | 3       | WP-13    | `run_id`                                                              | `\&#123; should_compact: bool, preserve_blocks, pin_memory, suggested_prompt \&#125;`                                 |
| `route`                 | routing | WP-26    | `AgentRequest, ContextBudgetPolicy`                                   | `RouteDecision`                                                                                                       |
| `route`                 | routing | WP-27    | `route_decision_id, validation_results, changed_files, rubric_status` | `VerificationEnvelope`                                                                                                |

The five existing MCP tools (context, check_plan, rescue, record_trace, run_rubric) and the nine
extended tools listed in `AGENT_README.md` are unchanged.

---

## 9. Migration order (for WP-02)

Apply in numeric order. Each migration is idempotent.

1. `v2_001_memory.sql`
2. `v2_002_lessons.sql`
3. `v2_003_context_budget.sql`
4. `v2_004_routing.sql`
5. `v2_005_postgres_pgvector.sql` — Postgres-only; guarded by `IF EXISTS`

Schema downgrade is **not** supported. To roll back, restore the pre-migration `.atelier/` directory
or the PostgreSQL backup captured before applying V2 migrations.

---

## 10. Reviewer checklist

Before any schema-touching PR is merged, the reviewer (`atelier:review`) must confirm:

- [ ] No new field is missing from this document
- [ ] No `.atelier/` mirror file format is changed without bumping the migration version
- [ ] No PII / chain-of-thought / secret can be persisted by the new fields (verify with
      `tests/security/test_redaction_extends_to_memory.py`)
- [ ] All new tables are mirrored in both SQLite and Postgres backends
- [ ] FTS5 index exists for any new `text`-bearing table
- [ ] All new Pydantic models have `extra="forbid"`
- [ ] All new `id` fields use `uuid7` (sortable) — see `infra/storage/ids.py:make_uuid7()`

---
