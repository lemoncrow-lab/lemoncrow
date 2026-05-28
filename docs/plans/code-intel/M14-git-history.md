# M14 — Git history index (deleted symbols, renames, temporal search)

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Blocked by M0. Independent of M1 (can run on local-adapter results).
> Blocks M15 (blame). Should ship before M11 bootstrap so the graveyard is
> warm before the first context job completes.

**No new MCP tool.** Extends `code op="search"` with `scope="deleted"` and
`since` / `touched_by` filters. All storage is a new table in the existing
`CodeContextEngine` SQLite DB, accessed via `engine.connection()` (engine
adds a public connection accessor as part of this milestone; no underscored
attribute access).

This milestone implements `SymbolIntelProvider` (introduced in M1) — the
graveyard is registered as a provider with `name="graveyard"` and the store
routes `scope="deleted"` queries to it.

## Goal

Make deleted, renamed, and moved symbols first-class citizens. Today, when a
symbol disappears the agent's only option is `git log --all --grep` — a text
search across commit messages that misses renames and doesn't know about
symbols at all. This milestone gives `code op="search" scope="deleted"` the
same sub-millisecond response the live index gives for present symbols.

## Background — why this is the most impactful gap

Google's code search indexes the full commit graph. SCIP indexes only HEAD.
Every developer has experienced "I know this function existed three months ago,
where did it go?" — currently unanswerable without manual git archaeology. This
milestone closes that gap cleanly and is largely independent of the rest of the
SCIP stack.

## Module layout

```
src/atelier/infra/code_intel/git_history/
  __init__.py
  AGENT_README.md
  walker.py        pygit2-based commit walker; populates the graveyard on first run
  renames.py       Tracks file renames via git diff --find-renames (similarity ≥ 70%)
  graveyard.py     SQLite-backed SymbolGraveyard: upsert, query, eviction
  adapter.py       SymbolIntelProvider impl; plugs into SymbolIntelStore
```

## Symbol Graveyard (`graveyard.py`)

New table in the `CodeContextEngine` SQLite DB (no new file):

```sql
CREATE TABLE IF NOT EXISTS symbol_graveyard (
    id               INTEGER PRIMARY KEY,
    symbol_name      TEXT NOT NULL,
    qualified_name   TEXT,
    file_path        TEXT NOT NULL,
    language         TEXT,
    deleted_at_sha   TEXT NOT NULL,
    deleted_at_ts    INTEGER NOT NULL,   -- unix epoch
    last_author      TEXT,
    last_commit_msg  TEXT,
    rename_target    TEXT,               -- new path if moved, NULL if deleted
    signature_hash   TEXT,               -- sha256 of last known signature
    UNIQUE(qualified_name, deleted_at_sha)
);
CREATE INDEX IF NOT EXISTS idx_graveyard_name ON symbol_graveyard(symbol_name);
CREATE INDEX IF NOT EXISTS idx_graveyard_ts   ON symbol_graveyard(deleted_at_ts);
```

`SymbolGraveyard` class exposes:
- `upsert(entry: GraveyardEntry)` — idempotent on `(qualified_name, sha)`.
- `find_deleted(query: str, since_ts: int | None, language: str | None) -> list[GraveyardEntry]`
  — FTS over `symbol_name || ' ' || qualified_name || ' ' || file_path`.
- `evict_before(ts: int)` — removes entries older than configurable horizon
  (default 2 years). Called during `index_repo`.

## Walker (`walker.py`)

Uses `pygit2` (libgit2 Python bindings, v1.19.2+, active maintenance):

```python
import pygit2

def walk_history(repo_path: str, graveyard: SymbolGraveyard,
                 since_sha: str | None = None) -> None:
    repo = pygit2.Repository(repo_path)
    for commit in repo.walk(repo.head.target, pygit2.GIT_SORT_TOPOLOGICAL):
        if since_sha and str(commit.id) == since_sha:
            break
        if not commit.parents:
            continue
        diff = commit.parents[0].peel().diff_to_tree(commit.peel())
        for patch in diff:
            if patch.delta.status != pygit2.GIT_DELTA_DELETED:
                continue
            # extract symbol names from the deleted file's last-known content
            # via tree-sitter tags (existing infra/tree_sitter/tags.py)
            symbols = extract_symbols_from_blob(patch.delta.old_file, repo)
            for sym in symbols:
                graveyard.upsert(GraveyardEntry(
                    symbol_name=sym.name,
                    qualified_name=sym.qualified_name,
                    file_path=patch.delta.old_file.path,
                    language=detect_language(patch.delta.old_file.path),
                    deleted_at_sha=str(commit.id),
                    deleted_at_ts=commit.commit_time,
                    last_author=str(commit.author.email),
                    last_commit_msg=commit.message[:200],
                ))
```

Full history walk runs as a **background job** via `core/service/jobs.py`
during M11 bootstrap. Incremental updates are triggered by a HEAD-poll
strategy (see open questions for trade-offs) — M1's file watcher cannot be
reused because file edits and commits are decoupled (e.g. `git pull` moves
HEAD without firing local file events).

## Renames (`renames.py`)

```python
diff = repo.diff(old_commit.peel(), new_commit.peel(),
                 flags=pygit2.GIT_DIFF_FIND_RENAMES)
diff.find_similar(threshold=70)  # 70% similarity triggers rename detection
for patch in diff:
    if patch.delta.status == pygit2.GIT_DELTA_RENAMED:
        graveyard.record_rename(
            old_path=patch.delta.old_file.path,
            new_path=patch.delta.new_file.path,
            at_sha=str(new_commit.id),
        )
```

Renames are stored as `rename_target` on the graveyard entry so queries can
answer *"this function moved to X"* rather than *"this function was deleted"*.

## New query surface on `tool_code`

```python
op: Literal[..., "search"],
# new params:
scope: Literal["repo", "deleted", "external"] = "repo",
since: str | None = None,          # "30d", "6m", "2025-01-01" — ISO date or relative
touched_by: str | None = None,     # email or display name substring
```

Dispatch inside `tool_code`:

```python
# Inside tool_code dispatch — graveyard is already registered with the store
# during engine startup; no per-call construction:
if op == "search" and scope == "deleted":
    return engine.intel_store.find_deleted(
        query=query,
        since_ts=parse_since(since),
        language=language,
        budget_tokens=budget_tokens,
    )
```

Engine startup wiring (in `CodeContextEngine.__init__` or a deferred init):

```python
graveyard = SymbolGraveyard(self.connection())   # public accessor
self.intel_store.register(GraveyardProvider(graveyard))
```

The `scope` literal is `Literal["repo", "deleted", "external"]` — there is
no `"all"`. Callers wanting a union issue two calls (the cache makes the
second one free).

Response shape:

```json
{
  "matches": [
    {
      "symbol_name": "AuthMiddleware",
      "qualified_name": "src.auth.middleware.AuthMiddleware",
      "file_path": "src/auth/middleware.py",
      "deleted_at": "2025-11-03T14:22:00Z",
      "deleted_at_sha": "abc123",
      "last_author": "pankaj@example.com",
      "last_commit_msg": "Remove legacy auth middleware, replaced by JWT",
      "rename_target": null,
      "provenance": "graveyard"
    }
  ],
  "cache_hit": false,
  "tokens_saved": 0
}
```

When `rename_target` is non-null:

```json
{
  "symbol_name": "process_payment",
  "rename_target": "src/payments/processor.py",
  "rename_note": "File moved; symbol may still exist at new path"
}
```

The `since` and `touched_by` filters also apply to live SCIP results when
`scope="repo"` — enabling temporal queries on the live index:

```python
code(op="search", query="payment", since="30d")
# → symbols modified in last 30 days, from git log + SCIP cross-reference
```

## Bootstrap integration (M11)

M11's first-context job gains a new step:

```python
# Step N: populate graveyard from full git history
jobs.submit(walk_history, repo_path=repo_root, graveyard=graveyard)
```

This is the slowest step on initial bootstrap for large repos with long
histories. Mitigations:
- Walk newest-first; stop after 2 years by default (configurable via
  `[code_intel.history] max_age_days = 730`).
- Run at lowest priority in the job queue.
- Persist progress checkpoint (`last_walked_sha`) so partial walks resume.

## Validation

Tests under `tests/infra/code_intel/git_history/`:

- `test_graveyard_upsert_and_query` — insert 3 graveyard entries, FTS finds
  each by name substring.
- `test_walker_finds_deleted_symbol` — git fixture with a file deleted in
  commit N → graveyard entry present after walk.
- `test_rename_recorded` — file renamed in commit N → `rename_target` set,
  not treated as deletion.
- `test_incremental_walk_only_new_commits` — walk once, add commit, walk again
  → only the new commit is processed.
- `test_scope_deleted_returns_graveyard_entries` — `tool_code(op="search",
  scope="deleted", query="AuthMiddleware")` → graveyard hit in response.
- `test_since_filter_excludes_old_entries` — entries older than `since` not
  returned.

Benchmark `tests/benchmarks/code_intel/bench_graveyard_query.py`:

- 10k graveyard entries; FTS query must complete in < 5ms.

## Exit criteria

- Full history walk completes on the Atelier repo itself without error.
- `code(op="search", scope="deleted", query="<known-deleted-symbol>")` returns
  the correct graveyard entry with deletion commit and author.
- Rename entries show `rename_target` instead of a bare deletion.
- Incremental walk triggered on new commit; checkpoint updates correctly.
- Token-cost benchmark row added to `tests/benchmarks/code_intel/`.
- Validation matrix row added.

## Open questions

- **Incremental trigger mechanism.** Three options, pick one on claim:
  (1) Poll `git rev-parse HEAD` every 30s from the existing job worker —
  zero install footprint, slight latency.
  (2) Install an Atelier-managed `.git/hooks/post-commit` hook — instant
  but requires hook ownership and may conflict with user hooks.
  (3) Watch `.git/HEAD` and `.git/refs/heads/` with `watchdog` — instant,
  no hook conflict, but misses bare-repo and worktree edge cases.
  Recommendation: start with (1) for the MVP; revisit if latency matters.
- **Binary file detection for blob parsing.** Use the existing
  `repo_map/graph.iter_source_files` extension allowlist (already filters
  binaries). Files outside the allowlist are skipped with no error.
- **Blob parsing cost.** Extracting symbols from deleted file blobs via
  tree-sitter is cheap but not free. Cap at 500 symbols per blob.
- **History depth default.** 2 years covers 95% of practical "where did it
  go?" queries. Make configurable; default aggressive (2 years) for small
  repos, conservative (6 months) for repos with >100k commits.
- **pygit2 vs gitpython.** pygit2 chosen: active maintenance (v1.19.2 March
  2026), C bindings (lower overhead in long-running process), Python 3.11–3.14.
  GitPython is in maintenance mode and has documented resource leak risks.
