# Phase 8: Context Lineage - Context

**Gathered:** 2025-07-15
**Status:** Ready for planning
**Mode:** Auto-generated (smart discuss — fully autonomous, all recommendations accepted)

<domain>
## Phase Boundary

Make every past commit a retrievable, ranked context chunk merged into existing `code op="search"` results. New module `summarizer.py` + `embedder.py` in `infra/code_intel/git_history/`; new `commit_chunks` SQLite table extending existing `code_context` DB; `engine.search_symbols()` merges commit chunks into candidate set with `provenance="commit"`. No new MCP tool — commits surface in existing `code op="search"` with a provenance filter.

</domain>

<decisions>
## Implementation Decisions

### Storage Schema
- `commit_chunks` table: `commit_sha TEXT PRIMARY KEY`, `author_date INTEGER`, `files_touched TEXT` (JSON array), `symbols_touched TEXT` (JSON, nullable), `summary TEXT` (≤200 tokens), `summary_model TEXT`, `embedding BLOB` (nullable), `index_version INTEGER`
- Use JSON array (not join table) for `files_touched` — simpler write path, LIKE-scanned; add join table in follow-up if profiling shows bottleneck
- Indexes: `idx_commit_author_date` on `author_date`; `idx_commit_files` on `files_touched`

### Summarizer Model Choice
- Default: **Haiku 3.5** for incremental summarisation (~600ms, ~$0.0001/commit)
- Configurable fallback: local SLM (Ollama qwen-2.5-7b) for local-first/privacy scenarios
- Frontier batch (overnight) available for one-time backfill of large histories
- Summary prompt is `_PROMPT_V1` (version-pinned): 80–120 words, objective + key files/functions + technical terms, no code, no PII

### Scope Trimming (M1)
- Skip `symbols_touched` extraction on M1 — skip SCIP delta extraction to reduce scope by ~½ day; add in follow-up
- Skip merge commits with no file-level diff
- Skip commits with >50 files touched unless message has `[lineage:keep]` tag
- Skip bot commits (Dependabot, Renovate) unless `[lineage:keep]` tag

### Retrieval Integration
- Commit chunks merged into `search_symbols()` with score penalty of −0.1 (configurable)
- Filter parameter: `code op="search" provenance="commit"` returns only commit chunks
- Each result carries `provenance ∈ {"symbol", "file", "commit"}` and `commit_sha` when applicable
- Default top-k rendering shows mix of provenance types

### Bootstrap & Incremental Update
- Bootstrap: background walk of last 500 commits on first `code op="search"` against a repo; resumable, progress persisted
- Incremental: post-commit hook in `integrations/claude/plugin/hooks/`; falls back to startup catch-up if hook missing

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/atelier/infra/code_intel/git_history/walker.py` — existing commit enumeration, produces structured commit records; extend to feed into summarizer
- `src/atelier/infra/code_intel/git_history/models.py` — extend with `CommitSummary`, `CommitChunk` dataclasses
- `src/atelier/core/capabilities/code_context/intel_store.py` — existing code_context SQLite store; add `commit_chunks` table to same DB
- `src/atelier/core/capabilities/code_context/engine.py` — extend `search_symbols()` to merge commit chunks
- Existing embedding infrastructure (same dim/ranker) — reuse for commit embeddings

### Established Patterns
- `models.py` uses frozen dataclasses (e.g., `GraveyardEntry`, `BlameRequest`, `BlameHunk`)
- `SymbolIntelProvider` protocol in `intel_store.py` — extend `search_symbols()` to include commit candidate set
- `intel_store.py` uses SQLite with same schema family

### Integration Points
- `engine.py` `search_symbols()` — merged candidate set, provenance tagging
- `integrations/claude/plugin/hooks/` — new post-commit hook for incremental update
- `model_routing` infrastructure — wire summarizer model preference through existing router

</code_context>

<specifics>
## Specific Ideas

- Reference: Augment Code Context Lineage — Gemini 2.0 Flash over each commit producing condensed prose summary; token-level injection cost similar to small file
- Benchmark target: ≥7/10 on 10 real bug-fix commits from this repo's history (baseline ≤2/10)
- Tests in `tests/infra/code_intel/git_history/`: `test_summarizer.py`, `test_embedder.py`, `test_search_merge.py`, `test_walker_resume.py`
- Benchmark in `tests/benchmarks/context_quality/M1_lineage.py`

</specifics>

<deferred>
## Deferred Ideas

- `symbols_touched` extraction via SCIP delta — deferred to follow-up phase to reduce M1 scope
- Join table for `files_touched` — deferred pending profiling evidence of LIKE-scan bottleneck
- Frontier nightly batch summarization for huge histories — available as config option, not default

</deferred>
