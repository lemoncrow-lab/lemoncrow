# Phase 8: Context Lineage — Research

**Researched:** 2026-07-15
**Domain:** LLM-summarised commit history + embedding, SQLite schema extension, semantic search merge
**Confidence:** HIGH — all findings verified by direct codebase inspection

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Storage Schema**
- `commit_chunks` table: `commit_sha TEXT PRIMARY KEY`, `author_date INTEGER`, `files_touched TEXT` (JSON array), `symbols_touched TEXT` (JSON, nullable), `summary TEXT` (≤200 tokens), `summary_model TEXT`, `embedding BLOB` (nullable), `index_version INTEGER`
- JSON array (not join table) for `files_touched` — LIKE-scanned; join table deferred
- Indexes: `idx_commit_author_date` on `author_date`; `idx_commit_files` on `files_touched`

**Summarizer Model Choice**
- Default: **Haiku 3.5** for incremental (~600ms, ~$0.0001/commit)
- Configurable fallback: local SLM (Ollama qwen-2.5-7b) for local-first/privacy scenarios
- Frontier batch (overnight) available for one-time backfill of large histories
- Summary prompt is `_PROMPT_V1` (version-pinned): 80–120 words, objective + key files/functions + technical terms, no code, no PII

**Scope Trimming (M1)**
- Skip `symbols_touched` extraction on M1 — SCIP delta extraction deferred to follow-up
- Skip merge commits with no file-level diff
- Skip commits with >50 files touched unless message has `[lineage:keep]` tag
- Skip bot commits (Dependabot, Renovate) unless `[lineage:keep]` tag

**Retrieval Integration**
- Commit chunks merged into `search_symbols()` with score penalty of −0.1 (configurable)
- Filter parameter: `code op="search" provenance="commit"` returns only commit chunks
- Each result carries `provenance ∈ {"symbol", "file", "commit"}` and `commit_sha` when applicable
- Default top-k rendering shows mix of provenance types

**Bootstrap & Incremental Update**
- Bootstrap: background walk of last 500 commits on first `code op="search"` against a repo; resumable, progress persisted
- Incremental: post-commit hook in `integrations/claude/plugin/hooks/`; falls back to startup catch-up if hook missing

### the agent's Discretion
(None specified — all implementation choices are locked above)

### Deferred Ideas (OUT OF SCOPE)
- `symbols_touched` extraction via SCIP delta — deferred to follow-up phase
- Join table for `files_touched` — deferred pending profiling evidence of LIKE-scan bottleneck
- Frontier nightly batch summarization for huge histories — available as config option, not default
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| LINEAGE-01 | Bootstrap walk summarises last 500 commits and persists to `commit_chunks` SQLite table; merge commits and commits with >50 files touched are skipped automatically | `walker.py` already uses pygit2 commit enumeration; `_init_schema` / `CREATE TABLE IF NOT EXISTS` pattern for additive schema; `engine_state` key for progress tracking |
| LINEAGE-02 | Incremental update fires on next session start when new commits exist; walk is resumable if interrupted mid-way | `engine_state` table stores resume cursor; `session_start.py` hook fires at startup; `_ensure_history_ready` in `adapter.py` shows the incremental pattern |
| LINEAGE-03 | `code op="search"` merges commit chunks with symbol/file results; each commit result carries `provenance="commit"` and `commit_sha` fields | `search_symbols()` merges lexical + semantic hits via `reciprocal_rank_fuse`; commit chunks become a third candidate source merged at the same stage |
| LINEAGE-04 | `code op="search" provenance="commit"` filter returns only commit chunk results | `scope` parameter in `tool_search` / `search_symbols` is the existing hook; a new `provenance` filter parameter follows the same dispatch pattern |
| LINEAGE-05 | Summariser uses version-pinned prompt (`_PROMPT_V1`); bumping the version triggers re-summarisation of all commits | `index_version` pattern in `engine_state` table; `summarizer.py` stores `summary_model` in row; query `WHERE index_version < current` to detect stale rows |
| LINEAGE-06 | Commit chunks get a small score penalty (configurable, default −0.1) so they don't crowd current-file results | Score field exists on `SymbolRecord`; penalty applied after `cosine_similarity` in commit search; `ATELIER_LINEAGE_COMMIT_SCORE_PENALTY` env var |
| CQEVAL-01 | `tests/benchmarks/context_quality/` suite exists with benchmark modules for M1–M4 and a README describing the internal eval protocol | `tests/benchmarks/code_intel/` is the established benchmark pattern; README + per-milestone benchmark file |
| CQEVAL-02 | M1 benchmark (`M1_lineage.py`): ≥7/10 commit history queries answered correctly (baseline ≤2/10 expected) | 425 real commits in the Atelier repo provide concrete history; benchmark picks 10 bug-fix commits and tests citation recall |
</phase_requirements>

---

## Summary

Phase 8 adds LLM-summarised commit history as a first-class context source inside the existing `code op="search"` pipeline. The implementation is purely additive — no existing tables or APIs are modified, only extended. The Atelier repo already has 425 commits (within the 500-commit bootstrap target), pygit2 already integrated, the internal LLM infrastructure (`infra/internal_llm/`) already provides an OpenAI-compatible `chat()` call, and the `LocalEmbedder` + cosine-similarity pipeline already exists.

The key architectural insight: the `DeletedHistorySearchAdapter` pattern in `adapter.py` is the canonical model for everything in this phase — an adapter that wraps SQLite state, stores a HEAD cursor in `engine_state`, detects stale state on `_ensure_history_ready`, and walks history lazily. `CommitLineageAdapter` (new) follows this exact pattern: check `engine_state` for `commit_lineage_head`, walk new commits, summarise via internal LLM, embed, persist. `search_symbols()` then pulls commit candidates alongside lexical/semantic symbol hits and fuses them.

**Primary recommendation:** Model `CommitLineageAdapter` on `DeletedHistorySearchAdapter`, store the resume cursor in `engine_state`, extend `search_symbols()` to pull commit candidates, and fuse with −0.1 penalty before the token-budget packer. Tests follow the `test_graveyard.py` pattern with a temporary git repo created by `subprocess.run(["git", ...])`.

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Commit enumeration + diff extraction | `infra/code_intel/git_history/` | — | Already owns pygit2; walker.py lives here |
| LLM summarisation of commit diffs | `infra/code_intel/git_history/summarizer.py` (new) | `infra/internal_llm/` | Summariser is infra-layer background processor; calls `infra/internal_llm/chat()` |
| Embedding of summaries | `infra/code_intel/git_history/embedder.py` (new) | `infra/embeddings/local.py` | Embedding is infra-layer; reuses `LocalEmbedder.embed()` |
| SQLite persistence (`commit_chunks` table) | `core/capabilities/code_context/engine.py` | — | Same DB as symbols, files, call_edges — same `_init_schema()` extension point |
| Commit search and merge | `core/capabilities/code_context/engine.py` | — | `search_symbols()` is the merge point; engine owns all candidate fusion |
| Bootstrap + incremental scheduling | `core/capabilities/code_context/engine.py` | `integrations/claude/plugin/hooks/` | Engine's `_ensure_lineage_ready()` mirrors `_ensure_history_ready()`; hook triggers incremental update |
| MCP filter parameter (`provenance="commit"`) | `gateway/adapters/mcp_server.py` (thin) | `core/capabilities/code_context/engine.py` | Gateway adds `provenance` param to `tool_code`; engine dispatches in `tool_search` |
| Benchmark harness | `tests/benchmarks/context_quality/` | — | Eval-only; no production coupling |

---

## Standard Stack

### Core (all already in pyproject.toml)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `pygit2` | 1.19.2 (pinned) | Git commit enumeration, diff extraction | [VERIFIED: pyproject.toml] Already used by walker.py, adapter.py, blame.py |
| `atelier.infra.internal_llm` | internal | LLM summarisation via `chat()` | [VERIFIED: codebase] `ATELIER_LLM_BACKEND=openai` → OpenAI-compatible; default Ollama |
| `atelier.infra.embeddings.local` | internal | 384-dim feature-hash embedding | [VERIFIED: codebase] `LocalEmbedder.dim=384`; used by `SemanticSearchRanker` |
| `atelier.infra.storage.vector` | internal | `cosine_similarity`, `generate_embedding`, embedding cache | [VERIFIED: codebase] Used by `SemanticSearchRanker._embed_text()` |
| `sqlite3` | stdlib | `commit_chunks` table in existing code_context DB | [VERIFIED: codebase] All code-intel state uses SQLite via `engine._connect()` |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `openai` (optional dep) | ≥1.0 | Haiku 3.5 calls via `ATELIER_LLM_BACKEND=openai` | When `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` set |
| `ollama` | ≥0.6.2 | Local SLM fallback (qwen-2.5-7b) | Default backend when no API key present |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `internal_llm.chat()` | Direct `anthropic` SDK | `internal_llm` is the established abstraction; adding a new direct SDK call breaks the abstraction |
| `LocalEmbedder` (384-dim) | `OpenAIEmbedder` (1536-dim) | Local embedder is deterministic, free, no API dependency — correct default for commit summaries; dim must match symbol embeddings in `SemanticSearchRanker` |
| `engine_state` for resume cursor | Separate resume table | `engine_state` is the existing KV store for this DB; adding a table for a single key would be over-engineering |

---

## Package Legitimacy Audit

> No new external packages are installed in this phase. All dependencies are already in `pyproject.toml` (pygit2==1.19.2, ollama>=0.6.2, openai>=1.0 optional). No package legitimacy gate needed.

**Packages removed due to slopcheck [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** none

---

## Architecture Patterns

### System Architecture Diagram

```
git repo (pygit2)
      │
      ▼
walker.py → _iter_commits(limit=500, since_sha=resume_cursor)
      │  skip merge commits (no file diff)
      │  skip >50-file commits (unless [lineage:keep])
      │  skip bot commits (Dependabot, Renovate)
      ▼
summarizer.py → _PROMPT_V1(commit_message, diff[:2000_tokens])
      │  → infra/internal_llm/chat(model="claude-haiku-4-5")
      │  → CommitSummary(sha, author_date, files_touched, summary, model)
      ▼
embedder.py → LocalEmbedder().embed([summary_text])[0]
      │  → embedding BLOB (384-dim float32 little-endian)
      ▼
engine.py (_init_schema) → commit_chunks SQLite table
      │  upsert row (INSERT OR REPLACE)
      │  update engine_state["commit_lineage_head"] = HEAD sha
      │
engine.py (search_symbols)
      │
      ├── lexical_hits ← intel_store.search_symbols()
      ├── semantic_hits ← _search_symbols_semantic_local()
      └── commit_hits ← _search_commit_chunks(query, limit) ← NEW
                            cosine_similarity(query_vec, row["embedding"])
                            apply score -= 0.1 penalty (configurable)
                            convert row → SymbolRecord(provenance="commit", commit_sha=...)
      │
      ▼
reciprocal_rank_fuse(lexical_hits, semantic_hits + commit_hits, limit)
      │
      ▼
tool_search() → _pack_items_payload() → MCP response
      │  provenance filter: if provenance_filter=="commit", return only commit hits
      ▼
mcp_server.py tool_code(op="search", provenance="commit") → filtered results
```

### Recommended Project Structure

```
src/atelier/infra/code_intel/git_history/
├── walker.py            (existing — add new iteration helper for bootstrap)
├── models.py            (existing — add CommitSummary, CommitChunk dataclasses)
├── summarizer.py        (NEW — commit → SemanticSummary via internal_llm.chat())
├── embedder.py          (NEW — summary → embedding vector via LocalEmbedder)
├── adapter.py           (existing — DeletedHistorySearchAdapter unchanged)
├── blame.py             (existing — unchanged)
├── graveyard.py         (existing — unchanged)
└── renames.py           (existing — unchanged)

src/atelier/core/capabilities/code_context/
├── engine.py            (extend — _init_schema adds commit_chunks; search_symbols merges commit hits)
├── intel_store.py       (existing SymbolIntelStore — unchanged)
├── models.py            (existing — SymbolRecord.provenance already supports "commit")
└── embedding.py         (existing — unchanged)

integrations/claude/plugin/hooks/
└── post_commit.py       (NEW — thin hook script for incremental update)

tests/infra/code_intel/git_history/
├── test_summarizer.py   (NEW)
├── test_embedder.py     (NEW)
├── test_search_merge.py (NEW)
└── test_walker_resume.py (NEW)

tests/benchmarks/context_quality/
├── README.md            (NEW)
└── M1_lineage.py        (NEW)
```

### Pattern 1: Schema Extension (additive table via `_init_schema`)

**What:** `engine.py._init_schema()` is called on every `connection()` call and uses `CREATE TABLE IF NOT EXISTS` — purely additive, no migration needed.

**When to use:** Adding a new table to the existing code_context DB.

**Example:**
```python
# Source: verified in engine.py:3670-3751
def _init_schema(self, conn: sqlite3.Connection) -> None:
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS engine_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS symbols (...);
        -- ADD commit_chunks here — safe, additive, no migration needed
        CREATE TABLE IF NOT EXISTS commit_chunks (
            commit_sha TEXT PRIMARY KEY,
            author_date INTEGER NOT NULL,
            files_touched TEXT NOT NULL,
            symbols_touched TEXT,
            summary TEXT NOT NULL,
            summary_model TEXT NOT NULL,
            embedding BLOB,
            index_version INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_commit_author_date ON commit_chunks(author_date);
        CREATE INDEX IF NOT EXISTS idx_commit_files ON commit_chunks(files_touched);
    """)
```

### Pattern 2: Resume Cursor via `engine_state`

**What:** `engine_state` KV table stores progress markers as text. `_bump_index_version` and `_ensure_history_ready` both use `INSERT OR IGNORE / ON CONFLICT DO UPDATE` for safe concurrent writes.

**Example:**
```python
# Source: verified in adapter.py:86-107 (_ensure_history_ready)
# and engine.py:5254-5266 (_bump_index_version)
def _ensure_lineage_ready(self) -> None:
    current_head = self._safe_current_head_sha()
    if current_head is None:
        return
    with closing(self._connection_factory()) as conn:
        self._init_schema(conn)
        row = conn.execute(
            "SELECT value FROM engine_state WHERE key = 'commit_lineage_head'"
        ).fetchone()
        previous_head = str(row["value"]) if row is not None else None
        count_row = conn.execute(
            "SELECT COUNT(*) AS n FROM commit_chunks"
        ).fetchone()
        chunk_count = int(count_row["n"]) if count_row is not None else 0
        if previous_head == current_head and chunk_count > 0:
            return
        # Bootstrap/incremental: walk + summarise + embed
        self._walk_and_summarise(conn, since_sha=previous_head)
        conn.execute(
            "INSERT INTO engine_state(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("commit_lineage_head", current_head),
        )
        conn.commit()
```

### Pattern 3: Candidate Merge in `search_symbols`

**What:** `search_symbols()` currently merges `lexical_hits` + `semantic_hits` via `_semantic_ranker.reciprocal_rank_fuse()`. Commit hits slot in as a third source. The `SymbolRecord.provenance` field already supports arbitrary string values.

**Example:**
```python
# Source: verified in engine.py:2397-2427
# Existing RRF fusion — extend to include commit_hits:
commit_hits = self._search_commit_chunks(query, limit=candidate_limit)
# commit_hits are SymbolRecord with provenance="commit", commit_sha=sha
hits = self._semantic_ranker.reciprocal_rank_fuse(
    lexical_hits,
    semantic_hits + commit_hits,  # merged candidate set
    limit=limit,
)
# provenance filter (new):
if provenance_filter == "commit":
    hits = [h for h in hits if h.provenance == "commit"]
```

### Pattern 4: Fake-LLM in Tests (monkeypatch)

**What:** Tests monkey-patch the `chat` function at the import path where the module-under-test imports it.

**Example:**
```python
# Source: verified in tests/gateway/test_cli.py:315-320
# and tests/core/test_local_sleeptime.py:95
def test_summarizer_produces_valid_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_chat(messages, *, model=None, json_schema=None):
        return "Refactored auth module to remove session leak in login flow."

    monkeypatch.setattr(
        "atelier.infra.code_intel.git_history.summarizer.chat",
        _fake_chat,
    )
    # ... test body
```

### Pattern 5: Temporary Git Repo Fixture

**What:** `tests/core/test_code_context.py` creates real git repos in `tmp_path` using `subprocess.run(["git", ...])`. Tests call `git init`, `git config`, write files, `git add -A`, `git commit`.

**Example:**
```python
# Source: verified in tests/core/test_code_context.py:31-63
def _create_history_fixture(tmp_path: Path) -> tuple[Path, str, str]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(["init"], repo_root)
    _git(["config", "user.name", "Fixture Tester"], repo_root)
    _git(["config", "user.email", "fixture@example.com"], repo_root)
    (repo_root / "legacy.py").write_text("class LegacyCheckout:\n    def process(self): return 1\n")
    _commit_all(repo_root, "add legacy symbol")
    # ... more commits
    return repo_root, sha1, sha2
```

### Anti-Patterns to Avoid

- **Directly calling `anthropic.Anthropic()` in `summarizer.py`:** Always go through `infra/internal_llm/chat()` — this is the established abstraction and allows Ollama fallback.
- **Storing embeddings as JSON arrays:** Use BLOB (`struct.pack('f' * dim, *vector)` little-endian float32) — consistent with how `vector_cache.sqlite` stores vectors.
- **Blocking `search_symbols()` on bootstrap:** Bootstrap must be a background operation; `search_symbols()` should return non-lineage results immediately if bootstrap hasn't completed.
- **Adding a `provenance` parameter to `SymbolRecord` pydantic model as required:** The field already exists with `provenance: str = "local"` — just populate it with `"commit"`.
- **Modifying `_SEARCH_REPO_STRIP_ITEM_KEYS`:** This frozenset strips `provenance` from repo-scope results. For commit chunks, `provenance` is NOT uniform — it MUST be preserved. Ensure commit results bypass the strip or are handled before compact.

---

## Module-by-Module Analysis

### `walker.py` (extend)

**Current state:** Walks commits looking for DELETED/RENAMED files to populate the graveyard. Uses `repo.walk(head.id, pygit2.enums.SortMode.TOPOLOGICAL)` and iterates all commits.

**What to add:** A new generator function `iter_commit_records(repo_path, *, limit=500, since_sha=None)` that:
1. Walks up to `limit` commits using `SortMode.TIME` (chronological, newest first)
2. Stops when `commit.id == since_sha` (resume support)
3. Yields `CommitRecord` dataclasses with: `sha`, `author_date`, `message`, `files_touched`, `is_merge`
4. Applies skip filters: `is_merge and len(diff_patches) == 0`, `len(files_touched) > 50` (unless `[lineage:keep]`), bot authors

**Key pygit2 API:** `commit.parents` (len > 1 = merge), `commit.author.email`, `commit.commit_time`, `parent.tree.diff_to_tree(commit.tree)`, `patch.delta.new_file.path`

**Risk:** The current `walk_history` uses TOPOLOGICAL sort; the new function should use TIME sort for chronological bootstrap. Both are available via `pygit2.enums.SortMode`.

### `models.py` (extend)

**Current state:** Contains `GraveyardEntry`, `BlameRequest`, `BlameHunk`, `ChurnStats`, `BlameRangeAnnotation` — all frozen dataclasses.

**What to add:**
```python
@dataclass(frozen=True)
class CommitRecord:
    sha: str
    author_date: int           # unix seconds
    message: str
    files_touched: list[str]
    is_merge: bool

@dataclass(frozen=True)  
class CommitSummary:
    sha: str
    author_date: int
    files_touched: list[str]
    summary: str               # ≤200 tokens
    summary_model: str         # e.g. "claude-haiku-4-5", "llama3.1:8b"
    prompt_version: str        # "v1"

@dataclass(frozen=True)
class CommitChunk:
    commit_sha: str
    author_date: int
    files_touched: list[str]   # JSON-deserialised
    symbols_touched: list[str] | None
    summary: str
    summary_model: str
    embedding: list[float] | None
    index_version: int
```

### `summarizer.py` (new)

**Purpose:** `CommitRecord → CommitSummary` via `internal_llm.chat()`.

**Key decisions:**
- `_PROMPT_V1` is a module-level constant (not a function) — allows `WHERE summary_model != 'v1:haiku-3.5'` queries to detect stale
- Model is configurable via `ATELIER_LINEAGE_MODEL` env var; defaults to `claude-haiku-4-5` when `ATELIER_LLM_BACKEND=openai`
- Diff truncated to ~2000 tokens before sending (use `count_tokens` from `core/capabilities/repo_map/budget`)
- Returns `CommitSummary` with `prompt_version="v1"` baked in
- On LLM failure: raises `SummarizerError` — caller decides whether to skip or retry

**Wiring to model routing:** The `ModelRouter` in `core/capabilities/model_routing/router.py` already has `cheap_model="claude-haiku-4-5"` as its default cheap tier (verified at line 133). The summarizer should use `ATELIER_LLM_BACKEND=openai` + `ATELIER_OPENAI_MODEL=claude-haiku-4-5` for Haiku, which routes through the existing `openai_client.py`. For Ollama, use `ATELIER_LLM_BACKEND=ollama` (default) + `ATELIER_OLLAMA_MODEL=qwen2.5:7b`.

### `embedder.py` (new)

**Purpose:** `CommitSummary → embedding vector` via `LocalEmbedder`.

**Key decisions:**
- Reuse `LocalEmbedder(dim=384)` — same embedder as `SemanticSearchRanker`; verified at `local.py:6`
- Text to embed: `f"{summary.summary}\n{' '.join(summary.files_touched[:10])}"` — include top files for better keyword overlap
- Store as BLOB: `struct.pack(f'{len(vector)}f', *vector)` in little-endian float32

**De-serialisation:** `struct.unpack(f'{len(blob)//4}f', blob)` → list[float]

### `intel_store.py` / `engine.py` (extend)

**`_init_schema()` extension:** Add `commit_chunks` table and indexes inside the existing `executescript()` call — idempotent via `CREATE TABLE IF NOT EXISTS`.

**New methods on `CodeContextEngine`:**
- `_ensure_lineage_ready()` — checks `engine_state["commit_lineage_head"]` vs HEAD; walks/summarises if stale
- `_search_commit_chunks(query, *, limit)` → `list[SymbolRecord]`
  - Embeds `query` via `SemanticSearchRanker._embed_query()` (reuse existing)
  - Scans `commit_chunks WHERE embedding IS NOT NULL`
  - Computes `cosine_similarity(query_vec, stored_vec)` per row
  - Applies `score -= LINEAGE_COMMIT_SCORE_PENALTY` (default 0.1, from `ATELIER_LINEAGE_COMMIT_SCORE_PENALTY`)
  - Returns `SymbolRecord` objects with `provenance="commit"`, `commit_sha=sha`, `symbol_name=sha[:8]`, `qualified_name=summary[:80]`, `file_path=files_touched[0] or ""`, `kind="commit"`
- `_walk_and_summarise(conn, *, since_sha)` — iterates `iter_commit_records`, calls summariser + embedder, upserts to `commit_chunks`

**`search_symbols()` extension:**
- For `scope="repo"` and `resolved_mode in ("semantic", "hybrid")`, add `commit_hits = self._search_commit_chunks(query, limit=candidate_limit)`
- Merge into `_semantic_ranker.reciprocal_rank_fuse(lexical_hits, semantic_hits + commit_hits, limit=limit)`
- New `provenance` filter param: if set to `"commit"`, filter hits before returning

**Bootstrap scheduling:** Call `_ensure_lineage_ready()` inside `_ensure_indexed()` (already called at search time), or as a background thread similar to autosync. Background threading is already established (`_start_autosync_worker` pattern).

### `mcp_server.py` (thin extension)

**What changes:** Add `provenance: str | None = None` parameter to `tool_code()` signature and pass it through to `engine.tool_search()`. This follows the existing pattern where `scope`, `since`, `touched_by` are forwarded.

**`tool_search()` extension:** Accept `provenance_filter: str | None = None`; after building `hits`, apply `if provenance_filter: hits = [h for h in hits if h.provenance == provenance_filter]`.

**`_SEARCH_REPO_STRIP_ITEM_KEYS` concern:** This frozenset includes `"provenance"` and strips it from repo-scope compacted items. Commit chunk items need `provenance` and `commit_sha` preserved. Solution: **don't strip `provenance` from items where `provenance == "commit"`**, or more simply — strip only when provenance is `"local"` (the uniform case). Implement in `_compact_search_items`: skip stripping for items with non-local provenance.

---

## Embedding Pipeline Details

| Property | Value | Source |
|----------|-------|--------|
| Embedder class | `LocalEmbedder` | `infra/embeddings/local.py` — verified |
| Embedding model | `"hashing"` (feature hashing) | `local.py:6 _DEFAULT_MODEL` — verified |
| Dimension | 384 | `local.py:7 _DEFAULT_DIM` — verified |
| Storage format | BLOB (float32 little-endian) | Consistent with `vector_cache.sqlite` pattern |
| Similarity metric | Cosine similarity | `infra/storage/vector.cosine_similarity()` — verified |
| Query embedding | `SemanticSearchRanker._embed_query()` | Uses `vector_cache_key` + `_embed_text()` — reuse directly |
| Embedding cache | `~/.atelier/vector_cache.sqlite` (query only) | Commit chunk vectors stored in `commit_chunks.embedding` BLOB |

**Why `LocalEmbedder` (not `OpenAIEmbedder`):** `SemanticSearchRanker` already uses `LocalEmbedder` for symbol embeddings. Commit chunks MUST use the same embedder and dimension so they are comparable in the fused ranking. Mixing dims would silently produce garbage similarity scores.

**Embedding text for commits:**
```python
f"{commit_summary.summary}\n{' '.join(commit_summary.files_touched[:10])}"
```
Including top files improves keyword recall for file-path queries ("when did utils.py change?").

---

## Migration Strategy

**There is no breaking migration.** The schema extension is purely additive:

1. `commit_chunks` table uses `CREATE TABLE IF NOT EXISTS` — safe on existing DBs.
2. Indexes use `CREATE INDEX IF NOT EXISTS` — safe.
3. No existing columns modified, no tables dropped.
4. `engine_state` key `"commit_lineage_head"` is new — no conflict with existing keys.

**Backward compatibility:** If `commit_chunks` table doesn't exist (old DB), `_ensure_lineage_ready()` creates it on first call. If the table exists but is empty, bootstrap runs. If HEAD cursor matches, no reprocessing. Resumability is implemented by:
- Storing `engine_state["commit_lineage_watermark"]` = last successfully processed SHA after each batch
- On restart, `iter_commit_records(since_sha=watermark)` skips already-processed commits
- Walk 500 commits max; process in batches of 50 with checkpoint writes

**Prompt version bump re-summarisation:** When `_PROMPT_V1` is bumped to `_PROMPT_V2`, increment `index_version`. Stale rows: `SELECT * FROM commit_chunks WHERE index_version < current_version`. These rows get re-summarised in the next background pass (do not block search; old summaries remain searchable until replaced).

---

## Test Patterns

### Fixture Pattern: Real Git Repo in `tmp_path`

All git-history tests create real git repos using `subprocess.run`. This is the established pattern — no mock git needed.

```python
# Source: verified in tests/core/test_code_context.py:31-79
def _git(args: list[str], repo_root: Path) -> str:
    return subprocess.run(["git", *args], cwd=repo_root, check=True,
                          capture_output=True, text=True).stdout.strip()

def _create_lineage_fixture(tmp_path: Path) -> tuple[Path, list[str]]:
    """3 commits: add feature, fix bug, refactor. Returns (repo_root, [sha1, sha2, sha3])."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(["init"], repo_root)
    _git(["config", "user.name", "Test Author"], repo_root)
    _git(["config", "user.email", "test@example.com"], repo_root)
    (repo_root / "auth.py").write_text("def login(): pass\n")
    _git(["add", "-A"], repo_root)
    _git(["commit", "-m", "feat: add login endpoint"], repo_root)
    sha1 = _git(["rev-parse", "HEAD"], repo_root)
    # ... more commits
    return repo_root, [sha1, sha2, sha3]
```

### Fixture Pattern: Fake LLM via monkeypatch

```python
# Source: verified in tests/gateway/test_cli.py:315-318 + tests/core/test_local_sleeptime.py:95
def test_summarizer_produces_valid_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "atelier.infra.code_intel.git_history.summarizer.chat",
        lambda messages, **kw: "Fixed authentication session leak in login flow.",
    )
```

### Fixture Pattern: SQLite in-memory for unit tests

```python
# Source: verified in tests/infra/code_intel/git_history/test_graveyard.py
graveyard = SymbolGraveyard(sqlite3.connect(":memory:"))
```
For `commit_chunks` unit tests: pass `:memory:` DB to `CodeContextEngine(db_path=":memory:")`.

### Test Files to Create

| File | What It Tests | Key Assertions |
|------|---------------|----------------|
| `test_summarizer.py` | Fake-LLM summary generation | Summary ≤200 tokens, no code snippets, `prompt_version="v1"` in result |
| `test_embedder.py` | Embedding of `CommitSummary` | Vector dim=384, BLOB roundtrip preserves values within float32 tolerance |
| `test_search_merge.py` | `engine.search_symbols()` returns commit hits | `provenance="commit"` on commit results; `commit_sha` field present; score < symbol_score for same query |
| `test_walker_resume.py` | Bootstrap resumability | Walk 5 commits, interrupt after 2 (watermark written), resume, verify all 5 processed once |

### `pytest.mark.slow` Convention

The `pyproject.toml` configures `addopts = "-ra --strict-markers -m 'not slow'"` — slow tests excluded by default. Mark integration/benchmark tests `@pytest.mark.slow`. Unit tests in `tests/infra/code_intel/git_history/` should NOT be marked slow (they use tiny in-memory git repos).

---

## Hook Integration Pattern

**Existing hooks** (verified in `hooks.json`): SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, PreCompact, PostCompact, Stop, SubagentStop.

**No `PostCommit` hook exists** in Claude Code's hook event system. The CONTEXT.md decision is:
> Incremental: a post-commit hook in `integrations/claude/plugin/hooks/`; **falls back to startup catch-up if the hook missing**.

This means the primary mechanism is **startup catch-up** (fired by `SessionStart`), not a git post-commit hook. The `session_start.py` hook already runs on every session start. The implementation should:

1. **Primary path:** `_ensure_lineage_ready()` called during `_ensure_indexed()` at first `code op="search"` — catches up on any new commits since last session.
2. **Optional git hook:** A shell script `integrations/claude/plugin/hooks/post_commit.sh` (not a Python hook event) that triggers `atelier code lineage-update` CLI command. This is NOT registered in `hooks.json` (which only contains Claude Code hook events); it would be installed via `scripts/install_claude.sh` into `.git/hooks/post-commit`.

**Hook script pattern** (verified by `session_start.py`):
```python
# All hooks: fail-open (exit 0 on any error), read payload from stdin, fail silently
def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    # ... work here
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

**For `post_commit.py`**: Simply trigger `_ensure_lineage_ready()` via subprocess call to `atelier` CLI, or call the internal API directly. Keep it thin and fail-open.

---

## Risk Assessment

### Risk 1: `_SEARCH_REPO_STRIP_ITEM_KEYS` Strips `provenance`
**What goes wrong:** `_compact_search_items()` calls `cleaned = {k: v for k, v in item.items() if k not in _SEARCH_REPO_STRIP_ITEM_KEYS}` — stripping `"provenance"` from repo-scope items. Commit chunk items lose their `provenance="commit"` field, breaking the provenance filter.
**Why it happens:** The frozenset was designed for symbol/file results where provenance is uniformly `"local"`.
**How to avoid:** In `_compact_search_items()`, conditionally skip stripping for commit-provenance items: `if item.get("provenance") != "local": skip strip`. Or: remove `"provenance"` from `_SEARCH_REPO_STRIP_ITEM_KEYS` and rely on top-level provenance aggregation only.
**Warning signs:** `provenance_filter="commit"` returns 0 results even when commit chunks exist.

### Risk 2: Embedding Dimension Mismatch
**What goes wrong:** If `LocalEmbedder` dim is overridden via `ATELIER_EMBEDDING_DIM` env var (default 384 in local.py but 1536 in `vector.py`), commit embeddings won't be comparable to symbol embeddings.
**Why it happens:** `LocalEmbedder` hardcodes `dim=384`; `generate_embedding()` in `vector.py` uses `ATELIER_EMBEDDING_DIM` (default 1536). These are different code paths.
**How to avoid:** `embedder.py` MUST instantiate `LocalEmbedder(dim=384)` directly (not via `make_embedder()`), same as `SemanticSearchRanker` does at `embedding.py:93`.
**Warning signs:** Cosine similarities all ≈ 0 or all ≈ 1; benchmark scores don't improve.

### Risk 3: Bootstrap Blocks First Search
**What goes wrong:** 500 commits × ~600ms/commit (Haiku) = ~5 minutes of blocking on first `code op="search"`.
**Why it happens:** `_ensure_lineage_ready()` called synchronously in `_ensure_indexed()`.
**How to avoid:** Run bootstrap as a daemon thread (same pattern as `_start_autosync_worker`). `search_symbols()` returns non-commit results immediately; commit results trickle in as background completes. Store `engine_state["commit_lineage_bootstrap_started"]` to avoid double-starting.
**Warning signs:** First `code op="search"` in a session takes >10 seconds.

### Risk 4: SQLite WAL Contention
**What goes wrong:** Background bootstrap thread writing to `commit_chunks` while search thread reads — SQLite WAL mode handles concurrent readers but background writes can slow down readers.
**Why it happens:** Single SQLite file; background thread holds write lock during batch upserts.
**How to avoid:** Use batch commits (every 50 commits); release connection after each batch. `engine._connect()` already uses `timeout=30.0`. WAL mode is already set in `_init_schema()` (`PRAGMA journal_mode=WAL`).
**Warning signs:** `sqlite3.OperationalError: database is locked` in test logs.

### Risk 5: Score Penalty Makes Commit Hits Unreachable
**What goes wrong:** With −0.1 penalty and top-k=20, commit hits may never appear in results if symbol results fill the list.
**Why it happens:** RRF merges by rank score; if all 20 symbol hits have higher RRF scores, commit hits are rank 21+.
**How to avoid:** RRF (Reciprocal Rank Fusion) is rank-based not score-based; the −0.1 penalty should be applied to the raw cosine score BEFORE RRF, affecting rank position but not directly comparing against symbol RRF scores. Alternative: reserve 2-3 slots for commit hits at the merge stage (positional injection).
**Warning signs:** M1 benchmark scores <3/10 even with committed summaries in DB.

### Risk 6: `tool_search()` Cache Invalidation
**What goes wrong:** Existing `RetrievalCache` caches `code.search` results keyed by query + index_version. After bootstrap completes mid-session, cached results don't include new commit chunks.
**Why it happens:** `index_version` doesn't change when `commit_chunks` grows (it only bumps on symbol reindex).
**How to avoid:** Add `commit_lineage_head` to the cache key for search queries, or bump `index_version` when bootstrap/incremental update completes (simplest).
**Warning signs:** `cache_hit=True` results never include commit chunks.

---

## Validation Architecture

> `workflow.nyquist_validation` not explicitly false in config — including this section.

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest (existing) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/infra/code_intel/git_history/ -q` |
| Full suite command | `uv run pytest tests/infra/code_intel/git_history/ tests/core/test_code_context.py -q` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| LINEAGE-01 | 500-commit bootstrap persists to `commit_chunks` without error | integration | `uv run pytest tests/infra/code_intel/git_history/test_walker_resume.py -q` | ❌ Wave 0 |
| LINEAGE-02 | Resumable walk; incremental update on new commits | integration | `uv run pytest tests/infra/code_intel/git_history/test_walker_resume.py -q` | ❌ Wave 0 |
| LINEAGE-03 | `search_symbols()` returns commit results with `provenance="commit"` and `commit_sha` | unit | `uv run pytest tests/infra/code_intel/git_history/test_search_merge.py -q` | ❌ Wave 0 |
| LINEAGE-04 | `provenance="commit"` filter returns only commit chunks | unit | `uv run pytest tests/infra/code_intel/git_history/test_search_merge.py::test_provenance_filter -q` | ❌ Wave 0 |
| LINEAGE-05 | `_PROMPT_V1` constant exists; bumping version triggers re-summarisation | unit | `uv run pytest tests/infra/code_intel/git_history/test_summarizer.py -q` | ❌ Wave 0 |
| LINEAGE-06 | Commit chunk score ≤ symbol score for same query | unit | `uv run pytest tests/infra/code_intel/git_history/test_search_merge.py::test_score_penalty -q` | ❌ Wave 0 |
| CQEVAL-01 | `tests/benchmarks/context_quality/` dir + README + M1_lineage.py | structure | `ls tests/benchmarks/context_quality/` | ❌ Wave 0 |
| CQEVAL-02 | ≥7/10 commit history queries answered with correct citation | benchmark | `uv run pytest tests/benchmarks/context_quality/M1_lineage.py -q -m slow` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/infra/code_intel/git_history/ -q`
- **Per wave merge:** `uv run pytest tests/infra/code_intel/git_history/ tests/core/test_code_context.py -q && make lint && make typecheck`
- **Phase gate:** Full suite green + CQEVAL-02 benchmark passes before `/gsd-verify-work`

### Wave 0 Gaps (all new files)
- [ ] `tests/infra/code_intel/git_history/test_summarizer.py` — covers LINEAGE-01, LINEAGE-05
- [ ] `tests/infra/code_intel/git_history/test_embedder.py` — covers LINEAGE-01
- [ ] `tests/infra/code_intel/git_history/test_search_merge.py` — covers LINEAGE-03, LINEAGE-04, LINEAGE-06
- [ ] `tests/infra/code_intel/git_history/test_walker_resume.py` — covers LINEAGE-01, LINEAGE-02
- [ ] `tests/benchmarks/context_quality/README.md` — covers CQEVAL-01
- [ ] `tests/benchmarks/context_quality/M1_lineage.py` — covers CQEVAL-02

---

## Security Domain

> Security enforcement is enabled (no explicit false in config).

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | Commit data is local-only |
| V3 Session Management | no | — |
| V4 Access Control | no | Single-user local tool |
| V5 Input Validation | yes | Commit messages/diffs sanitised before LLM prompt construction; truncate diff to 2000 tokens; strip null bytes |
| V6 Cryptography | no | Embeddings are not secrets |

### Known Threat Patterns

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Prompt injection via commit message | Tampering | XML-tag encapsulation of `<COMMIT_MESSAGE>` in `_PROMPT_V1`; message truncated to 500 chars |
| Path traversal in `files_touched` | Information Disclosure | All file paths stored as relative strings; `_safe_relpath()` pattern from engine.py used during extraction |
| SQLite injection via commit SHA | Tampering | Use parameterised queries only; never f-string interpolation in SQL |
| LLM response containing PII | Information Disclosure | `_PROMPT_V1` explicitly instructs "Do not include PII"; summary stored in plaintext — acceptable for local-only tool |

---

## Code Examples

### Commit record iteration (new pattern for `walker.py`)

```python
# New generator in walker.py — not yet written
def iter_commit_records(
    repo_path: str | Path,
    *,
    limit: int = 500,
    since_sha: str | None = None,
) -> Generator[CommitRecord, None, None]:
    pygit2 = require_pygit2()
    repo = pygit2.Repository(str(repo_path))
    head = repo.revparse_single("HEAD")
    count = 0
    for commit in repo.walk(head.id, pygit2.enums.SortMode.TIME):
        if since_sha and str(commit.id) == since_sha:
            break
        if count >= limit:
            break
        if not commit.parents:
            count += 1
            yield CommitRecord(sha=str(commit.id), ...)
            continue
        parent = commit.parents[0]
        diff = parent.tree.diff_to_tree(commit.tree)
        files_touched = [p.delta.new_file.path or p.delta.old_file.path for p in diff]
        is_merge = len(commit.parents) > 1
        count += 1
        yield CommitRecord(
            sha=str(commit.id),
            author_date=commit.commit_time,
            message=commit.message.strip()[:500],
            files_touched=files_touched,
            is_merge=is_merge,
        )
```

### Schema extension in `_init_schema()`

```python
# Add to the executescript in engine.py:3671
CREATE TABLE IF NOT EXISTS commit_chunks (
    commit_sha TEXT PRIMARY KEY,
    author_date INTEGER NOT NULL,
    files_touched TEXT NOT NULL,
    symbols_touched TEXT,
    summary TEXT NOT NULL,
    summary_model TEXT NOT NULL,
    embedding BLOB,
    index_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commit_author_date ON commit_chunks(author_date);
CREATE INDEX IF NOT EXISTS idx_commit_files ON commit_chunks(files_touched);
```

### Search commit chunks

```python
# New method on CodeContextEngine
def _search_commit_chunks(
    self,
    query: str,
    *,
    limit: int = 20,
) -> list[SymbolRecord]:
    query_vector = self._semantic_ranker._embed_query(query)
    if not query_vector:
        return []
    penalty = float(os.environ.get("ATELIER_LINEAGE_COMMIT_SCORE_PENALTY", "0.1"))
    with self._connect() as conn:
        self._init_schema(conn)
        rows = conn.execute(
            "SELECT commit_sha, author_date, files_touched, summary, embedding "
            "FROM commit_chunks WHERE embedding IS NOT NULL "
            "ORDER BY author_date DESC LIMIT ?",
            (limit * 5,),  # scan more candidates
        ).fetchall()
    from atelier.infra.storage.vector import cosine_similarity
    import struct, json
    scored: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        blob = row["embedding"]
        n = len(blob) // 4
        vec = list(struct.unpack(f"{n}f", blob))
        score = cosine_similarity(query_vector, vec) - penalty
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda x: -x[0])
    results = []
    for score, row in scored[:limit]:
        files = json.loads(row["files_touched"])
        results.append(SymbolRecord(
            symbol_id=f"commit:{row['commit_sha']}",
            repo_id=self.repo_id,
            file_path=files[0] if files else "",
            language="text",
            symbol_name=row["commit_sha"][:8],
            qualified_name=row["summary"][:80],
            kind="commit",
            signature=row["summary"][:120],
            start_byte=0, end_byte=0, start_line=0, end_line=0,
            content_hash=row["commit_sha"],
            score=score,
            provenance="commit",
        ))
    return results
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Raw `git log` parsing by LLM | Pre-embedded commit summaries | This phase (new) | Avoids raw git output parsing; queries are O(1) cosine similarity |
| All search results are symbols/files | Mixed provenance (symbol, file, commit) in one ranked list | This phase (new) | Agent answers "why was X changed?" without separate tool call |
| `walker.py` walks for graveyard only | `walker.py` serves both graveyard AND commit lineage bootstrap | This phase (new) | Reuse of existing pygit2 iteration logic |

**Deprecated/outdated:**
- Nothing deprecated — this is purely additive.

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `LocalEmbedder.dim=384` produces embeddings comparable with `SemanticSearchRanker` (both use same embedder) | Embedding Pipeline | If `SemanticSearchRanker` uses a different embedder in prod, cosine similarity between commit and symbol embeddings is meaningless |
| A2 | `claude-haiku-4-5` is available via `ATELIER_LLM_BACKEND=openai` + OpenRouter/Anthropic API | Standard Stack | If model name differs (e.g., `claude-haiku-3-5`), summariser calls fail; easily fixed by env var |
| A3 | `reciprocal_rank_fuse()` accepts heterogeneous `SymbolRecord` lists (symbols + commit chunks as same type) | Architecture Patterns | If RRF expects uniform types, need wrapper type; but `SymbolRecord` already has all required fields |
| A4 | The 425 commits in the Atelier repo provide sufficient benchmark data for CQEVAL-02 (10 queries) | Validation Architecture | 425 < 500 limit — all commits bootstrap; but if fewer than 10 are substantive bug-fix commits the benchmark is trivially easy |

**If this table is empty:** All claims verified by codebase inspection in this session. Only A1–A4 above are assumed without runtime verification.

---

## Open Questions

1. **`_SEARCH_REPO_STRIP_ITEM_KEYS` handling for commit provenance**
   - What we know: `_compact_search_items()` strips `provenance` from repo-scope results because it's always `"local"` — verified in engine.py:5308-5309
   - What's unclear: Best fix — remove `provenance` from the strip set entirely, or conditional skip
   - Recommendation: Remove `"provenance"` from `_SEARCH_REPO_STRIP_ITEM_KEYS` and let `_provenance_breakdown()` handle provenance aggregation. Lower risk than conditional logic.

2. **Background bootstrap threading vs. call-path bootstrap**
   - What we know: autosync uses a daemon thread (`_start_autosync_worker`); graveyard adapter uses call-path lazy init (`_ensure_history_ready`)
   - What's unclear: Bootstrap for 425 commits via Haiku takes ~4-5 minutes; call-path would block first search
   - Recommendation: Use daemon thread pattern (like autosync) with `threading.Event` for stop signal; call-path path only calls `_ensure_lineage_ready()` which either starts the thread or checks completion.

3. **Cache invalidation when commit chunks grow**
   - What we know: `RetrievalCache` is keyed by `index_version` (verified at engine.py:2292)
   - What's unclear: Whether to bump `index_version` on lineage update (affects ALL cache entries) or use a separate `commit_lineage_version` cache key
   - Recommendation: Add `commit_lineage_head` SHA to the `code.search` cache key args dict — precise invalidation without global cache bust.

4. **M1 benchmark commit selection**
   - What we know: Repo has 425 commits; benchmark needs 10 bug-fix commits
   - What's unclear: Whether 10 suitable bug-fix commits can be automatically selected via commit message grep ("fix", "bug") or need manual curation
   - Recommendation: Automate selection via `[Ff]ix` in commit message; fallback to most-changed-files commits. Manual override list in `M1_lineage.py`.

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| `pygit2` | Commit enumeration | ✓ | 1.19.2 (pinned) | None — required |
| `ollama` | Local SLM fallback | ✓ (package installed) | ≥0.6.2 | OpenAI-compatible API |
| `openai` package | Haiku 3.5 calls | Optional | ≥1.0 | Ollama (default) |
| `struct` (stdlib) | BLOB serialization | ✓ | stdlib | — |
| `json` (stdlib) | files_touched serialization | ✓ | stdlib | — |

**Missing dependencies with no fallback:** None — pygit2 is pinned and present; local embedder requires no external service.

**Missing dependencies with fallback:** `openai` package (optional dep, `atelier[cloud]`) — without it, summariser falls back to Ollama.

---

## Sources

### Primary (HIGH confidence — direct codebase inspection)
- `src/atelier/infra/code_intel/git_history/walker.py` — pygit2 API, commit iteration, diff extraction
- `src/atelier/infra/code_intel/git_history/adapter.py` — `DeletedHistorySearchAdapter`, resume cursor pattern, `_ensure_history_ready`
- `src/atelier/infra/code_intel/git_history/graveyard.py` — `SymbolGraveyard`, `CREATE TABLE IF NOT EXISTS` pattern, upsert pattern
- `src/atelier/core/capabilities/code_context/engine.py` — `_init_schema`, `search_symbols`, `_bump_index_version`, autosync worker, `_dedupe_search_items`, `_compact_search_items`, `_SEARCH_REPO_STRIP_ITEM_KEYS`
- `src/atelier/core/capabilities/code_context/models.py` — `SymbolRecord` fields including `provenance`, `score`
- `src/atelier/core/capabilities/code_context/embedding.py` — `SemanticSearchRanker`, `LocalEmbedder` usage, `reciprocal_rank_fuse`
- `src/atelier/infra/embeddings/local.py` — `dim=384`, `_DEFAULT_MODEL="hashing"`
- `src/atelier/infra/embeddings/factory.py` — embedder selection logic
- `src/atelier/infra/internal_llm/__init__.py` — `chat()`, `summarize()`, backend selection
- `src/atelier/gateway/adapters/mcp_server.py` — `tool_code()` signature, `op="search"` dispatch
- `tests/core/test_code_context.py` — git fixture pattern, SQLite test pattern
- `tests/infra/code_intel/git_history/test_graveyard.py` — SQLite in-memory pattern
- `tests/gateway/test_cli.py` — monkeypatch fake-LLM pattern
- `integrations/claude/plugin/hooks/hooks.json` — available hook events
- `integrations/claude/plugin/hooks/session_start.py` — hook script pattern (fail-open, stdin JSON)
- `pyproject.toml` — dependencies, test config, markers

### Secondary (MEDIUM confidence)
- `docs/plans/context-quality-lift/M1-context-lineage.md` — detailed design spec
- `docs/plans/context-quality-lift/grounding.md` — research grounding

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — verified by direct file inspection, all packages confirmed in pyproject.toml
- Architecture: HIGH — patterns extracted from running code, not assumed
- Pitfalls: HIGH — identified from direct reading of `_SEARCH_REPO_STRIP_ITEM_KEYS`, embedding dim inconsistency risk, autosync threading pattern

**Research date:** 2026-07-15
**Valid until:** 2026-08-15 (stable codebase; extend if major refactor happens)

---

## RESEARCH COMPLETE
