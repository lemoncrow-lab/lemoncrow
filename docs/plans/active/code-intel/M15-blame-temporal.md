# M15 — Blame + temporal annotation (`code op="blame"`)

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Blocked by M14 (pygit2 infrastructure) and M1 (SCIP byte ranges).
> Independent of M16–M18.

**No new MCP tool.** Adds `code op="blame"` to `tool_code`. All data is
derived from `pygit2.blame()` composed with SCIP symbol byte positions and
stored in an annotation table in the existing `CodeContextEngine` SQLite DB.

### Invariant: SCIP index version must match current HEAD

SCIP byte/line offsets are recorded at index time. `pygit2.blame()` runs on
current HEAD. If `index_version` is older than `git rev-parse HEAD`, the
symbol's line range used by blame is stale and may point to wrong lines (or
out of file). Two enforcement points:

1. **On every `op="blame"` call:** compare `engine.intel_store.scip.index_sha`
   against `git rev-parse HEAD`. If they differ, raise `StaleIndexError` and
   return `{"error": "index_stale", "hint": "run code op=\"index\" first"}`
   instead of bogus blame.
2. **On reindex (M1 watcher):** invalidate the entire `symbol_blame_cache`
   for the affected files (`DELETE WHERE file_path IN (...)`).

Document this in the response shape — agents must treat blame as a snapshot
tied to a specific index version, not a live view of git.

## Goal

Answer *"who last touched this symbol, when, and how often does it change?"*
in a single tool call. Today an agent must: (1) locate the symbol, (2) find
its line numbers, (3) shell out to `git blame`, (4) parse the output — four
steps consuming several hundred tokens. This milestone collapses it to one.

The churn score is the highest-signal field: a symbol that has changed 8 times
in 6 months is an unstable API; one that hasn't changed in 2 years is safe to
refactor without fear.

## Module layout

```
src/atelier/infra/code_intel/git_history/
  blame.py         BlameAnnotator — pygit2.blame() + SCIP byte ranges → annotation
  models.py        BlameAnnotation dataclass + ChurnStats
```

(Lives in the `git_history/` module introduced by M14.)

## BlameAnnotator (`blame.py`)

```python
import pygit2
from atelier.core.capabilities.code_context.engine import CodeContextEngine

class BlameAnnotator:
    def __init__(self, repo_path: str, engine: CodeContextEngine):
        self._repo = pygit2.Repository(repo_path)
        self._engine = engine

    def annotate(self, symbol_id: str | None = None,
                 query: str | None = None) -> BlameAnnotation:
        # 1. Resolve symbol → file_path + byte_start + byte_end via SCIP
        sym = self._engine.get_symbol(symbol_id or query)
        if not sym:
            raise SymbolNotFound(query)

        # 2. Convert byte range → line range
        line_start, line_end = bytes_to_lines(sym.file_path, sym.byte_start,
                                              sym.byte_end)

        # 3. pygit2 blame on the file, narrow to the symbol's line range
        blame = self._repo.blame(
            sym.file_path,
            min_line=line_start,
            max_line=line_end,
        )

        # 4. Aggregate hunks into a single annotation
        hunks = sorted(blame, key=lambda h: h.final_commit_id.hex)
        last_hunk = max(blame, key=lambda h: h.final_signature.time)
        last_commit = self._repo.get(last_hunk.final_commit_id)

        # 5. Compute churn: number of distinct commits touching these lines
        #    in the last 180 days
        churn = self._compute_churn(sym.file_path, line_start, line_end)

        return BlameAnnotation(
            symbol_name=sym.name,
            qualified_name=sym.qualified_name,
            file_path=sym.file_path,
            last_modified=last_commit.commit_time,
            last_author=str(last_commit.author.email),
            last_commit_sha=last_commit.hex[:12],
            last_commit_msg=last_commit.message.splitlines()[0][:120],
            age_days=days_since(last_commit.commit_time),
            churn_score=churn.score,        # float 0.0–1.0
            churn_commits_180d=churn.count,
            distinct_authors=len({h.final_signature.email for h in blame}),
        )

    def _compute_churn(self, file_path, line_start, line_end) -> ChurnStats:
        cutoff = int(time.time()) - 180 * 86400
        count = 0
        for commit in self._repo.walk(self._repo.head.target,
                                      pygit2.GIT_SORT_TIME):
            if commit.commit_time < cutoff:
                break
            if self._commit_touches_lines(commit, file_path,
                                          line_start, line_end):
                count += 1
        score = min(1.0, count / 20.0)   # normalise: 20 commits = max churn
        return ChurnStats(count=count, score=score)
```

## Annotation cache

Blame results are cached in a new table in the existing SQLite DB:

```sql
CREATE TABLE IF NOT EXISTS symbol_blame_cache (
    symbol_id     TEXT PRIMARY KEY,
    payload_json  TEXT NOT NULL,
    cached_at     INTEGER NOT NULL,
    index_version TEXT NOT NULL
);
```

TTL: 24h or index version bump (same invalidation logic as M0
`RetrievalCache`). Blame is stable within a day; recomputing on every call
would be wasteful.

## New op on `tool_code`

```python
op: Literal[..., "blame"],
# params reused: query (symbol name or id), budget_tokens
# new:
include_churn: bool = True,   # set False to skip 180d git-log walk (faster)
```

Dispatch:

```python
if op == "blame":
    from atelier.infra.code_intel.git_history.blame import BlameAnnotator
    annotator = BlameAnnotator(repo_root, engine)
    result = annotator.annotate(query=query)
    return budget_pack(result, budget_tokens, provenance="blame")
```

Response shape:

```json
{
  "symbol_name": "AuthService.verify",
  "qualified_name": "src.auth.service.AuthService.verify",
  "file_path": "src/auth/service.py",
  "last_modified": "2025-11-03T14:22:00Z",
  "last_author": "pankaj@example.com",
  "last_commit_sha": "abc123def456",
  "last_commit_msg": "Refactor auth to use JWT",
  "age_days": 197,
  "churn_score": 0.65,
  "churn_commits_180d": 13,
  "distinct_authors": 3,
  "provenance": "blame"
}
```

**Interpreting `churn_score`:**
- `< 0.2` — stable; safe to refactor with confidence
- `0.2–0.6` — active development; check with author before large changes
- `> 0.6` — hotspot; high risk of merge conflicts; flag in agent trace

## Temporal filters on `code op="search"` (bonus: wired here, not M14)

M14 adds `since` and `touched_by` params to the search op. This milestone
wires them to the live SCIP index (not just the graveyard):

```python
code(op="search", query="payment", since="30d")
# Returns symbols whose file_path appears in `git log --since=30d --name-only`
# Cross-referenced against SCIP symbol list to return actual symbol hits.

code(op="search", query="auth", touched_by="pankaj")
# Returns symbols in files last-committed-to by that author substring.
```

Implementation: pre-compute a `recently_changed_files` set from pygit2 log
(cached for 1h), then filter SCIP search results by file membership.

## Validation

Tests under `tests/infra/code_intel/git_history/`:

- `test_blame_returns_correct_last_author` — fixture repo with known commit
  history → `annotate("known_function")` returns correct author + sha.
- `test_churn_score_high_for_frequently_changed` — fixture function edited 15
  times in last 180 days → `churn_score > 0.6`.
- `test_churn_score_low_for_stable` — fixture function not edited in 1 year →
  `churn_score < 0.1`.
- `test_blame_cache_hit` — same symbol queried twice → second call returns
  `cache_hit=True`.
- `test_blame_invalidated_on_index_bump` — `index_repo` bumps version →
  cache miss on next blame call.
- `test_search_since_filter` — `code(op="search", query=X, since="7d")` →
  only symbols in files changed in last 7 days returned.

Benchmark `tests/benchmarks/code_intel/bench_blame.py`:

- Cold blame (no cache): < 50ms for a symbol in a 50k-commit repo.
- Hot blame (cache hit): < 2ms.

## Exit criteria

- `code(op="blame", query="<known-symbol>")` returns all fields including
  `churn_score`.
- Churn score is accurate against known commit history in the Atelier repo.
- Blame cache invalidates correctly on new commit.
- `since` filter on `code op="search"` returns only files changed since date.
- Validation matrix row added.

## Bootstrap pre-computation (M11)

During the M11 first-context job, pre-compute churn for the top-200 ranked
symbols (PageRank from `repo_map`) and write them into `symbol_blame_cache`.
Cold `op="blame"` calls for those symbols then return in <2ms. The
pre-computation runs after SCIP is warm and graveyard walk is at least 50%
done so the most-referenced symbols are resolvable.

## Open questions

- **`include_churn=False` escape hatch.** Churn scan is a 180d git log walk;
  on repos with 500k+ commits this can be slow even with early exit. Default
  `include_churn=True`; document the cost in `AGENT_README.md`.
- **Byte-to-line conversion.** SCIP stores byte offsets; `pygit2.blame`
  operates on line numbers. The conversion requires reading the file at the
  indexed SHA. Cache the file content per commit to avoid repeated reads.
- **What happens on uncommitted local edits?** `pygit2.blame` reports the
  blame for the last committed state; local edits are not attributed. Either
  (a) refuse blame on files with unstaged changes, or (b) annotate with
  `local_edits=True`. Pick on claim — (b) is more useful.
