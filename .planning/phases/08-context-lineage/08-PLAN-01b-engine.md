---
phase: 08
plan: "01b"
title: "Engine Integration — Schema, Bootstrap, Search, Hook, Tests"
status: pending
created: 2025-07-15
requires:
  - 08-PLAN-01a-infra.md
requirements:
  - LINEAGE-01
  - LINEAGE-02
  - LINEAGE-03
  - LINEAGE-04
  - LINEAGE-05
  - LINEAGE-06
---

# Phase 08 — Context Lineage: Engine Integration

## Goal

Integrate commit lineage into the existing `code op="search"` pipeline: extend `engine.py` with schema + bootstrap + search merge, wire the provenance filter, fix the strip-keys bug, add the post-commit hook, and write the unit test suite.

---

## Tasks
### Task 1: Extend `engine.py` — schema, bootstrap, and commit chunk search

**File:** `src/atelier/core/capabilities/code_context/engine.py` (extend)

**Why:** `engine.py` owns the SQLite DB (`_init_schema`), the bootstrap lifecycle, and the search pipeline. All new lineage persistence and retrieval goes here to stay in the same DB and reuse `_connect()`, `engine_state`, and `_safe_current_head_sha()` patterns.

**What:**

**A. Add module-level constants** (near the top, after existing `_LOCAL_PROVENANCE`):

```python
_LINEAGE_INDEX_VERSION = 1  # bump when _PROMPT_V1 is replaced with _PROMPT_V2
_LINEAGE_DEFAULT_SCORE_PENALTY = 0.1  # ATELIER_LINEAGE_COMMIT_SCORE_PENALTY overrides
```

**B. Add instance variables** in `__init__` (after `self._autosync_thread`):

```python
self._lineage_thread: threading.Thread | None = None
self._lineage_score_penalty: float = float(
    os.getenv("ATELIER_LINEAGE_COMMIT_SCORE_PENALTY", str(_LINEAGE_DEFAULT_SCORE_PENALTY))
)
```

**C. Extend `_init_schema()`** — append inside the `conn.executescript("""...""")` call, after the last existing `CREATE INDEX`:

```sql
CREATE TABLE IF NOT EXISTS commit_chunks (
    commit_sha     TEXT PRIMARY KEY,
    author_date    INTEGER NOT NULL,
    files_touched  TEXT NOT NULL,
    symbols_touched TEXT,
    summary        TEXT NOT NULL,
    summary_model  TEXT NOT NULL,
    embedding      BLOB,
    index_version  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commit_author_date ON commit_chunks(author_date);
CREATE INDEX IF NOT EXISTS idx_commit_files ON commit_chunks(files_touched);
```

Ensure this is inside the existing triple-quoted string in `executescript()` — do NOT create a separate `executescript` call.

**D. Add `_ensure_lineage_ready()` method** (add before `_deleted_history_adapter()`):

```python
def _ensure_lineage_ready(self) -> None:
    """Start background lineage bootstrap if commit_chunks is empty or stale.

    Non-blocking: launches a daemon thread identical to _start_autosync_worker.
    Safe to call multiple times — will not double-start.
    """
    if self._lineage_thread is not None:
        return  # already started
    current_head = self._safe_current_head_sha()
    if current_head is None:
        return  # not a git repo or pygit2 unavailable
    needs_update = False
    with contextlib.suppress(Exception):
        with closing(self._connect()) as conn:
            self._init_schema(conn)
            head_row = conn.execute(
                "SELECT value FROM engine_state WHERE key = 'commit_lineage_head'"
            ).fetchone()
            previous_head = str(head_row["value"]) if head_row is not None else None
            count_row = conn.execute(
                "SELECT COUNT(*) AS n FROM commit_chunks"
            ).fetchone()
            chunk_count = int(count_row["n"]) if count_row is not None else 0
            stale_row = conn.execute(
                "SELECT COUNT(*) AS n FROM commit_chunks WHERE index_version < ?",
                (_LINEAGE_INDEX_VERSION,),
            ).fetchone()
            has_stale = stale_row is not None and int(stale_row["n"]) > 0
            if previous_head != current_head or chunk_count == 0 or has_stale:
                needs_update = True
    if not needs_update:
        return
    self._lineage_thread = threading.Thread(
        target=self._lineage_bootstrap_worker,
        name=f"atelier-lineage-{self.repo_id[:8]}",
        daemon=True,
    )
    self._lineage_thread.start()
```

**E. Add `_lineage_bootstrap_worker()` method**:

```python
def _lineage_bootstrap_worker(self) -> None:
    """Background thread: walk, summarise, embed, persist commit chunks."""
    try:
        with closing(self._connect()) as conn:
            self._init_schema(conn)
            watermark_row = conn.execute(
                "SELECT value FROM engine_state WHERE key = 'commit_lineage_watermark'"
            ).fetchone()
            since_sha = str(watermark_row["value"]) if watermark_row is not None else None
        self._walk_and_summarise(since_sha=since_sha)
    except Exception:
        pass  # fail-open — lineage is additive, never blocks search
```

**F. Add `_walk_and_summarise()` method**:

```python
def _walk_and_summarise(self, *, since_sha: str | None) -> None:
    """Walk commits, summarise, embed, upsert to commit_chunks in batches of 50."""
    from atelier.infra.code_intel.git_history.walker import iter_commit_records
    from atelier.infra.code_intel.git_history.summarizer import summarize_commit, SummarizerError
    from atelier.infra.code_intel.git_history.embedder import embed_summary
    from atelier.infra.code_intel.git_history import require_pygit2

    import json

    # Extract diff text for a commit using pygit2
    def _get_diff_text(repo: Any, commit: Any) -> str:
        try:
            if not commit.parents:
                return ""
            parent = commit.parents[0]
            diff = parent.tree.diff_to_tree(commit.tree)
            return diff.patch or ""
        except Exception:
            return ""

    pygit2 = require_pygit2()
    repo = pygit2.Repository(str(self.repo_root))
    batch: list[tuple] = []

    for record in iter_commit_records(self.repo_root, limit=500, since_sha=since_sha):
        # Get diff text for this commit
        try:
            commit_obj = repo.revparse_single(record.sha)
            diff_text = _get_diff_text(repo, commit_obj)
        except Exception:
            diff_text = ""

        try:
            summary = summarize_commit(record, diff_text=diff_text)
            embedding_blob = embed_summary(summary)
        except SummarizerError:
            continue  # skip commits that fail summarisation
        except Exception:
            continue

        batch.append((
            summary.sha,
            summary.author_date,
            json.dumps(summary.files_touched),
            None,  # symbols_touched — deferred to follow-up phase
            summary.summary,
            summary.summary_model,
            embedding_blob,
            _LINEAGE_INDEX_VERSION,
        ))

        if len(batch) >= 50:
            self._flush_commit_batch(batch, watermark_sha=batch[-1][0])
            batch.clear()

    if batch:
        self._flush_commit_batch(batch, watermark_sha=batch[-1][0])

    # Mark HEAD as fully processed
    current_head = self._safe_current_head_sha()
    if current_head:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO engine_state(key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("commit_lineage_head", current_head),
            )
            conn.commit()
```

**G. Add `_flush_commit_batch()` method**:

```python
def _flush_commit_batch(
    self, batch: list[tuple], *, watermark_sha: str
) -> None:
    """Upsert a batch of commit chunks and advance the resume watermark."""
    with closing(self._connect()) as conn:
        self._init_schema(conn)
        conn.executemany(
            """INSERT OR REPLACE INTO commit_chunks
               (commit_sha, author_date, files_touched, symbols_touched,
                summary, summary_model, embedding, index_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            batch,
        )
        conn.execute(
            "INSERT INTO engine_state(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("commit_lineage_watermark", watermark_sha),
        )
        conn.commit()
```

**H. Add `_search_commit_chunks()` method**:

```python
def _search_commit_chunks(
    self,
    query: str,
    *,
    limit: int = 20,
) -> list[SymbolRecord]:
    """Embed `query` and return top-`limit` commit chunks as SymbolRecord objects.

    Each result has provenance="commit" and commit_sha set.
    Applies ATELIER_LINEAGE_COMMIT_SCORE_PENALTY (default 0.1) to the score.
    Returns [] if commit_chunks table is empty or no embeddings exist.
    """
    from atelier.infra.code_intel.git_history.embedder import decode_embedding
    from atelier.infra.storage.vector import cosine_similarity
    import json

    query_vec: list[float] | None = None
    with contextlib.suppress(Exception):
        # Reuse SemanticSearchRanker's embedding path for the query
        query_vec = self._semantic_ranker._embed_text(query)

    if not query_vec:
        return []

    rows: list[sqlite3.Row] = []
    with contextlib.suppress(Exception):
        with closing(self._connect()) as conn:
            self._init_schema(conn)
            rows = conn.execute(
                "SELECT commit_sha, author_date, files_touched, summary, summary_model, embedding "
                "FROM commit_chunks WHERE embedding IS NOT NULL "
                "ORDER BY author_date DESC LIMIT 2000"
            ).fetchall()

    if not rows:
        return []

    scored: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        try:
            stored_vec = decode_embedding(bytes(row["embedding"]))
            sim = cosine_similarity(query_vec, stored_vec)
            adjusted = sim - self._lineage_score_penalty
            scored.append((adjusted, row))
        except Exception:
            continue

    scored.sort(key=lambda t: t[0], reverse=True)
    top = scored[:limit]

    results: list[SymbolRecord] = []
    for score_val, row in top:
        try:
            files = json.loads(row["files_touched"]) if row["files_touched"] else []
            primary_file = files[0] if files else ""
            sha = str(row["commit_sha"])
            results.append(
                SymbolRecord(
                    symbol_id=sha,
                    repo_id=self.repo_id,
                    file_path=primary_file,
                    language="",
                    symbol_name=sha[:8],
                    qualified_name=str(row["summary"])[:80],
                    kind="commit",
                    signature=str(row["summary"]),
                    start_byte=0,
                    end_byte=0,
                    start_line=0,
                    end_line=0,
                    content_hash=sha,
                    score=round(score_val, 4),
                    provenance="commit",
                    commit_sha=sha,
                )
            )
        except Exception:
            continue
    return results
```

**I. Call `_ensure_lineage_ready()` from `_ensure_indexed()`** — find `_ensure_indexed` (line ~3796) and add one line before the method returns (not inside any lock, just before the final `return`):

```python
# At the end of _ensure_indexed(), before return:
self._ensure_lineage_ready()
```

**Test:**
```bash
# Schema test
uv run python -c "
import sqlite3, pathlib, tempfile
import sys; sys.path.insert(0, 'src')
from atelier.core.capabilities.code_context.engine import CodeContextEngine

td = pathlib.Path(tempfile.mkdtemp())
import subprocess
subprocess.run(['git','init'], cwd=td, check=True, capture_output=True)
subprocess.run(['git','config','user.name','T'], cwd=td, check=True, capture_output=True)
subprocess.run(['git','config','user.email','t@t.com'], cwd=td, check=True, capture_output=True)
(td/'a.py').write_text('x=1')
subprocess.run(['git','add','-A'], cwd=td, check=True, capture_output=True)
subprocess.run(['git','commit','-m','init'], cwd=td, check=True, capture_output=True)

e = CodeContextEngine(repo_root=td, repo_id='test', db_path=td/'.atelier'/'code.db')
with e.connection() as conn:
    tables = {r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()}
    assert 'commit_chunks' in tables, f'missing commit_chunks, got {tables}'
    print('commit_chunks table OK')
"
```

**Depends on:** Tasks 2, 3, 4

---

### Task 2: Extend `search_symbols()` + `tool_search()`, fix provenance stripping, extend `mcp_server.py`

**Files:**
- `src/atelier/core/capabilities/code_context/engine.py` (extend — same file as Task 5)
- `src/atelier/gateway/adapters/mcp_server.py` (extend)

**Why:** `search_symbols()` must merge commit hits into the fused result list (LINEAGE-03, LINEAGE-06). The `_compact_search_items` method currently strips `provenance` from ALL repo-scope items via `_SEARCH_REPO_STRIP_ITEM_KEYS`, breaking LINEAGE-03/04. `tool_code()` needs a `provenance` parameter so the agent can filter to `provenance="commit"` results (LINEAGE-04). `commit_sha` must survive compaction so it reaches the MCP caller.

**What:**

**A. Fix `_SEARCH_REPO_STRIP_ITEM_KEYS` stripping in `_compact_search_items()`** (line ~5309):

Current code:
```python
cleaned = {k: v for k, v in item.items() if k not in _SEARCH_REPO_STRIP_ITEM_KEYS}
```

Replace with:
```python
# For commit chunks, provenance and commit_sha must be preserved.
if item.get("provenance") == "commit":
    cleaned = {k: v for k, v in item.items()
               if k not in _SEARCH_REPO_STRIP_ITEM_KEYS or k == "provenance"}
else:
    cleaned = {k: v for k, v in item.items() if k not in _SEARCH_REPO_STRIP_ITEM_KEYS}
```

**B. Add `commit_sha` to `_SEARCH_COMPACT_DEFAULT_KEYS`** (line ~154):

Current:
```python
_SEARCH_COMPACT_DEFAULT_KEYS = set([*_SEARCH_ESSENTIAL_KEYS, "score"])
```

Replace with:
```python
_SEARCH_COMPACT_DEFAULT_KEYS = set([*_SEARCH_ESSENTIAL_KEYS, "score", "commit_sha"])
```

**C. Extend `search_symbols()` to merge commit hits** — in the `else:` branch handling semantic/hybrid scope (lines ~2396–2427), immediately after:
```python
semantic_hits = self._search_symbols_semantic_local(...)
hits = (
    semantic_hits[:limit]
    if resolved_mode == "semantic"
    else self._semantic_ranker.reciprocal_rank_fuse(lexical_hits, semantic_hits, limit=limit)
)
```

Replace that block with:

```python
semantic_hits = self._search_symbols_semantic_local(
    query,
    limit=candidate_limit,
    kind=kind,
    language=language,
)
# Merge commit chunks as a third candidate source (LINEAGE-03)
commit_hits: list[SymbolRecord] = []
with contextlib.suppress(Exception):
    commit_hits = self._search_commit_chunks(query, limit=candidate_limit)
if resolved_mode == "semantic":
    hits = (semantic_hits + commit_hits)[:limit]
else:
    hits = self._semantic_ranker.reciprocal_rank_fuse(
        lexical_hits, semantic_hits + commit_hits, limit=limit
    )
```

**D. Add `provenance_filter` parameter to the concrete `search_symbols()` overload** (lines ~2343–2427):

Add `provenance_filter: str | None = None` to the signature. After the `if file_glob:` block and before `return [self._attach_snippet(...)]`, add:

```python
# Provenance filter (LINEAGE-04)
if provenance_filter is not None:
    hits = [h for h in hits if h.provenance == provenance_filter]
```

Also update the two `@overload` stubs to include `provenance_filter: str | None = None`.

**E. Add `provenance_filter` parameter to `tool_search()`** (line ~808) — add `provenance_filter: str | None = None` to the signature, and pass it through to `search_symbols()`:

```python
raw_items = self.search_symbols(
    query,
    ...,
    provenance_filter=provenance_filter,  # add this kwarg
    auto_index=False,
)
```

**F. Extend `mcp_server.py tool_code()`** (line ~3480):

Add `provenance: str | None = None` to the `tool_code()` parameter list (near `scope`). In the `if op == "search":` block (line ~3570), add `provenance` to `search_kwargs`:

```python
if provenance is not None:
    search_kwargs["provenance_filter"] = provenance
```

Then pass `**search_kwargs` to `engine.tool_search(...)` (verify this is already how it's called; if not, pass explicitly).

**Test:**
```bash
# Note: test_search_merge.py is created in Task 4 of this plan. Run this after Task 4:
uv run pytest tests/infra/code_intel/git_history/test_search_merge.py -x -v
# Also verify the fix directly (can run immediately after Task 2):
uv run python -c "
from atelier.core.capabilities.code_context.engine import _SEARCH_REPO_STRIP_ITEM_KEYS, _SEARCH_COMPACT_DEFAULT_KEYS
assert 'commit_sha' in _SEARCH_COMPACT_DEFAULT_KEYS, 'commit_sha must be in compact keys'
# The strip set should still strip provenance for local items (backward compat)
# but _compact_search_items logic now conditionally skips for commit items
print('Key sets OK')
"
```

**Depends on:** Task 1 (this plan)

---

### Task 3: Add incremental update hook

**Files:**
- `integrations/claude/plugin/hooks/post_commit.py` (new)

**Why:** LINEAGE-02 requires incremental update on new commits. Per the research, Claude Code has no `PostCommit` hook event in `hooks.json` — the primary mechanism is the startup catch-up path already wired in Task 5 (`_ensure_lineage_ready()` called from `_ensure_indexed()` on every `code op="search"`). This task adds an optional git post-commit hook script that can accelerate updates when Claude session is active.

**What:**

Create `integrations/claude/plugin/hooks/post_commit.py` as a thin fail-open script. Unlike session_start.py (which handles a Claude hook event), this is designed to be called by a git post-commit hook script (`scripts/install_claude.sh` can wire it), **not** by Claude Code's hook event system:

```python
#!/usr/bin/env python3
"""Optional post-commit hook to trigger lineage incremental update.

Install into .git/hooks/post-commit (via scripts/install_claude.sh):
    echo 'python /path/to/post_commit.py' >> .git/hooks/post-commit

Falls back gracefully if atelier is not installed or the DB is locked.
The primary incremental path is startup catch-up via _ensure_lineage_ready()
called during code op="search". This hook only accelerates updates when
Claude is actively running.

Fail-open: exit 0 always — git commit must not be blocked by this hook.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    try:
        repo_root = os.getcwd()
        # Trigger lineage update by importing and calling the engine
        # This is a no-op if the engine is not initialised for this repo.
        from atelier.core.capabilities.code_context.engine import CodeContextEngine
        import pathlib

        atelier_root = pathlib.Path(
            os.environ.get("ATELIER_ROOT") or pathlib.Path.home() / ".atelier"
        )
        import hashlib
        repo_id = hashlib.sha256(repo_root.encode()).hexdigest()[:16]
        db_path = atelier_root / "repos" / repo_id / "code.db"
        if not db_path.exists():
            return 0  # no DB for this repo yet; startup catch-up will handle it

        engine = CodeContextEngine(
            repo_root=pathlib.Path(repo_root),
            repo_id=repo_id,
            db_path=db_path,
        )
        engine._ensure_lineage_ready()
    except Exception:
        pass  # always fail-open
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**Note:** The `session_start.py` hook does **not** need modification; the lineage startup catch-up is handled by `_ensure_lineage_ready()` being called on first `code op="search"` (wired in Task 5 via `_ensure_indexed()`). The SessionStart hook fires too early — before any `CodeContextEngine` is instantiated for the current workspace.

**Test:**
```bash
uv run python integrations/claude/plugin/hooks/post_commit.py
echo "Exit code: $?"
# Must print nothing and exit 0 (no DB exists in this context)
```

**Depends on:** Task 1 (this plan)

---

### Task 4: Write unit tests

**Files:**
- `tests/infra/code_intel/git_history/test_summarizer.py` (new)
- `tests/infra/code_intel/git_history/test_embedder.py` (new)
- `tests/infra/code_intel/git_history/test_search_merge.py` (new)
- `tests/infra/code_intel/git_history/test_walker_resume.py` (new)

**Why:** All four test files are specified in both CONTEXT.md and the M1 spec. They verify each module in isolation using in-memory SQLite and minimal git repos.

**What:**

**`test_summarizer.py`** — Tests `summarize_commit()` using monkeypatched `chat`:

```python
# Tests to include:
# 1. test_summary_returns_valid_CommitSummary — monkeypatch chat to return 100-word text;
#    assert result.prompt_version == "v1", result.sha == record.sha, result.summary == stripped text
# 2. test_summary_no_code_in_output — monkeypatch returns text with "def foo():" embedded;
#    assert summary does NOT contain "def foo():" (prompt constrains this) — test that
#    the prompt is passed correctly (actually just verify prompt_version and model fields)
# 3. test_summarizer_error_on_empty_response — monkeypatch returns "";
#    assert raises SummarizerError
# 4. test_summarizer_error_on_llm_exception — monkeypatch raises RuntimeError;
#    assert raises SummarizerError wrapping original error
# 5. test_summarizer_uses_env_model — set ATELIER_LINEAGE_MODEL="test-model";
#    monkeypatch chat to capture model kwarg; assert model == "test-model"
```

Monkeypatch path: `"atelier.infra.code_intel.git_history.summarizer.chat"`.

**`test_embedder.py`** — Tests `embed_summary()` / `decode_embedding()`:

```python
# 1. test_embed_returns_1536_bytes — embed a CommitSummary; assert len(blob) == 384*4
# 2. test_decode_roundtrip — embed then decode; assert len(decoded) == 384
# 3. test_embed_includes_files_in_text — two summaries identical text, different files_touched;
#    they should produce different vectors (because files are appended to embed text)
#    assert embed_summary(s1) != embed_summary(s2)
# 4. test_embedding_dim_constant — assert embedding_dim() == 384
```

**`test_search_merge.py`** — Integration test with real SQLite in-memory engine:

```python
# Fixture: create a small CodeContextEngine with db_path=":memory:" or tmp_path.
# Pre-seed commit_chunks with 2 rows using _flush_commit_batch() or direct INSERT.
# Each row has a real embedding from embed_summary().
#
# 1. test_search_returns_commit_hits — call engine.search_symbols("auth session leak");
#    assert any result.provenance == "commit"
# 2. test_commit_result_has_commit_sha — for each commit result, assert result.commit_sha is not None
# 3. test_provenance_filter_commit_only — call engine.search_symbols(query, provenance_filter="commit");
#    assert all(r.provenance == "commit" for r in results)
# 4. test_commit_score_has_penalty — seed a commit with perfect cosine similarity (same vector as query);
#    assert commit result score < 1.0 (penalty applied)
# 5. test_commit_sha_survives_tool_search — call engine.tool_search(query);
#    deserialise result["matches"]; find a commit item; assert "commit_sha" in item
```

**`test_walker_resume.py`** — Tests interrupt + resume:

```python
# Fixture: create a real git repo with 5 commits using subprocess.run(["git", ...]).
# Each commit adds one file.
#
# 1. test_iter_all_5_commits — iter_commit_records(repo, limit=500) yields exactly 5
# 2. test_skip_merge_commit — create a merge commit with no diff patches;
#    verify it is NOT yielded
# 3. test_skip_over_50_files_commit — create a commit touching 51 files;
#    verify it is skipped
# 4. test_lineage_keep_overrides_skip — same 51-file commit but message has "[lineage:keep]";
#    verify it IS yielded
# 5. test_resume_since_sha — iter_commit_records(limit=500, since_sha=sha_of_3rd_commit)
#    should yield only commits 4 and 5 (the 2 newer ones)
# 6. test_bot_commit_skip — commit with author email "bot@dependabot.github.com";
#    verify skipped
```

Run all tests:
```bash
uv run pytest tests/infra/code_intel/git_history/test_summarizer.py \
    tests/infra/code_intel/git_history/test_embedder.py \
    tests/infra/code_intel/git_history/test_search_merge.py \
    tests/infra/code_intel/git_history/test_walker_resume.py \
    -v --tb=short
```

All tests must pass with no `slow` markers — these are unit tests with tiny repos.

**Depends on:** Tasks 1–6

---

## Verification

```bash
# 1. All unit tests pass
uv run pytest tests/infra/code_intel/git_history/ -v --tb=short

# 2. Schema contains commit_chunks table
uv run python -c "
import pathlib, sqlite3
db = pathlib.Path.home() / '.atelier' / 'repos' / 'code.db'
if db.exists():
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()}
    print('Tables:', tables)
    assert 'commit_chunks' in tables
    print('PASS')
else:
    print('DB not yet created — run a code op=search first')
"

# 3. SymbolRecord accepts commit_sha field
uv run python -c "
from atelier.core.capabilities.code_context.models import SymbolRecord
r = SymbolRecord(symbol_id='s', repo_id='r', file_path='f.py', language='py',
    symbol_name='fn', qualified_name='fn', kind='function', signature='def fn()',
    start_byte=0, end_byte=10, start_line=1, end_line=1, content_hash='h',
    commit_sha='abc123')
assert r.commit_sha == 'abc123'
print('PASS')
"

# 4. Lineage constants present
uv run python -c "
from atelier.infra.code_intel.git_history.summarizer import _PROMPT_V1, _CURRENT_PROMPT_VERSION
assert _CURRENT_PROMPT_VERSION == 'v1'
assert '{message}' in _PROMPT_V1
print('Prompt version pinned OK')
"

# 5. mypy (if configured)
mypy src/atelier/infra/code_intel/git_history/summarizer.py src/atelier/infra/code_intel/git_history/embedder.py --ignore-missing-imports 2>&1 | tail -5
```

## Success Criteria

- LINEAGE-01: `commit_chunks` table exists; bootstrap walk processes up to 500 commits; merge commits and >50-file commits are skipped by the iterator skip logic.
- LINEAGE-02: `_ensure_lineage_ready()` detects new HEAD vs `commit_lineage_head` in `engine_state`; watermark key `commit_lineage_watermark` enables resume after interruption.
- LINEAGE-03: `search_symbols(query, mode="hybrid")` returns a mixed list; commit results have `provenance="commit"` and `commit_sha` set.
- LINEAGE-04: `search_symbols(query, provenance_filter="commit")` returns ONLY commit results; `tool_code(op="search", provenance="commit")` passes filter through.
- LINEAGE-05: `_CURRENT_PROMPT_VERSION = "v1"` in `summarizer.py`; `_LINEAGE_INDEX_VERSION = 1` in `engine.py`; stale detection query uses `WHERE index_version < _LINEAGE_INDEX_VERSION`.
- LINEAGE-06: `_lineage_score_penalty` defaults to 0.1; configurable via `ATELIER_LINEAGE_COMMIT_SCORE_PENALTY`; applied in `_search_commit_chunks()`.
- All 4 test files pass with zero failures.
