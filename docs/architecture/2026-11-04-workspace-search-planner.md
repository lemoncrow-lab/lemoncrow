# Workspace search architecture review (2026-11-04)

## Decision

Keep one durable SQLite/FTS index **per Git checkout**. Do not migrate to a
workspace-wide database or distributed search service. Add a small workspace
*catalogue and query planner* that reduces unnecessary per-repository work,
while preserving explicit coverage and correctness.

## Measured evidence

- Real `~/Projects/Olaryn`: 23 discovered repositories, 55,665 tracked files,
  13 existing indexes totaling ~1.64 GB. Membership discovery ~1.4 ms;
  dependency graph cold ~3.10 s, warm ~0.29 s with no scan truncation.
- `Beseam/pipelines/shop-corpus` alone has an ~891 MB index; ~859 MB is FTS
  content and FTS data. `Beseam/pipelines/fit-data/catalog` adds ~167 MB
  of predominantly dataset indexing. These are valid repositories but not
  necessarily useful for every code-search question.
- `LiteMcp._workspace_search` currently runs `LiteIndex.search` sequentially
  for every selected member. Its TODO acknowledges that local BM25 scores
  are compared as if directly calibrated across repositories.
- At 64 synthetic repositories, broad global queries are materially slower
  than path-scoped queries. Eight concurrent whole-workspace search callers
  often have worse throughput and tail latency than four.
- Repeated index emptiness checks opened a **second** SQLite connection for
  each non-matching member. A separate small fast-path fix now derives that
  fact from the active search connection. Two paired 64-repo runs measured
  no-hit median latency from 38.87 / 33.63 ms to 24.39 / 24.39 ms.
  These are synthetic measurements, not a general latency guarantee.

## Suggested architecture

1. **Workspace catalogue (metadata only).** Maintain root-relative repository
   identities, Git common directories, checkout branch, available index
   generation, package/language hints, and role (code, dataset, mixed).
   Derive from existing per-repository indexes and manifests; do not duplicate
   all source text. Keep state in LemonCrow's own user/runtime state rather
   than writing into a non-Git workspace root.
2. **Progressive query planning.** Respect explicit `paths` exactly. For
   unscoped queries, prioritize repositories based on path/package names,
   recent touched repositories and dependency edges. Search a small first
   wave; expand when necessary. A fast partial result must **report the
   skipped repositories** as `complete=false`; an exhaustive mode must remain
   available and is needed for release/security/impact reviews.
3. **Bounded global fanout.** Use one shared concurrency budget across client
   calls with a modest number of SQLite readers, not `N calls × N repos`
   unbounded parallelism. Measure P50/P95/P99, not just aggregate throughput.
4. **Rank fusion.** Merge top candidates with normalized evidence and
   reciprocal-rank-like fusion rather than treating each repo's BM25 score
   as globally comparable. Require relevance fixtures covering API owners,
   client call sites, tests, and dataset-vs-code ambiguity.
5. **Index policy by repository role.** Keep datasets accessible by explicit
   path/opt-in, but do not make their large JSON/CSV corpora first-class
   candidates for every source-code query. Preserve the option for
   researchers to search them deliberately.
6. **Incremental freshness and safety.** Catalogue invalidation tracks member
   TTL, index generations, worktree ownership and repo additions/removals.
   Keep untracked/dirty worktree changes reachable, and never silently claim
   complete coverage when an index failed, timed out, or a membership cap hit.

## Prioritized validation

- First validate the existing low-risk `index_empty` fast path under small
  and large synthetic workspaces and explicit empty-index fallbacks.
- Prototype a metadata-only catalogue and optional fast search. Compare it
  against exhaustive search on grounded cross-repo queries; collect relevance
  loss as well as latency and token savings.
- Add 1/4/8 concurrent clients and 25/50/64 member fixtures to CI's
  performance lane, with limits on descriptors, RSS and partial-result truth.
- Separately run a real Olaryn multi-hour soak and full cold-indexing test
  with appropriate resource limits before making production-scale claims.

Do **not** respond to slow global searches by immediately increasing per-repo
worker counts or building a large centralized all-content index: both can
increase contention and disk churn without improving answer quality.
