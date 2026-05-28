# M1 — Context Lineage (semantic commit history)

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).

## Goal

Make every past commit a retrievable, ranked context chunk so the agent can answer:

- "Why was this code changed?"
- "Is there a prior pattern in this repo for the thing I'm about to build?"
- "When did this regression first appear?"

without asking the host LLM to read raw `git log` output.

## Augment reference

[Context Lineage announcement](https://www.augmentcode.com/blog/announcing-context-lineage): Augment runs Gemini 2.0 Flash over each commit to produce a condensed prose summary (objective, key files/functions, technical terminology), chunks and embeds the summary, and injects relevant commit summaries into agent prompts "at token-level cost similar to a small file."

## Background — why this is the highest-leverage gap

SCIP (already integrated) and tree-sitter (already integrated) index HEAD. They are blind to history. The agent has to either (a) run `git log`/`git blame` and read raw output (expensive, noisy) or (b) hallucinate a history-free plan.

Atelier's `infra/code_intel/git_history/walker.py` already enumerates commits. It produces structured commit records but does not summarise or embed them. M1 closes that gap with the smallest possible delta.

## Module layout

```
src/atelier/infra/code_intel/git_history/
  walker.py             (existing) — enumerate commits, deltas
  summarizer.py         (new)      — commit → SemanticSummary via small LLM
  embedder.py           (new)      — summary → vector, persist via intel_store
  models.py             (existing) — add CommitSummary, CommitChunk
src/atelier/core/capabilities/code_context/
  intel_store.py        (extend)   — new table commit_chunks; same schema family as symbol chunks
  engine.py             (extend)   — search_symbols() merges commit_chunks into candidate set with provenance="commit"
```

No new MCP tool. Commit results land inside existing `code op="search"` results with a `provenance="commit"` field. Callers can filter; default rendering shows top-k regardless of provenance.

## Storage

New SQLite table inside the existing `code_context` DB:

```sql
CREATE TABLE commit_chunks (
  commit_sha TEXT PRIMARY KEY,
  author_date INTEGER NOT NULL,           -- unix seconds
  files_touched TEXT NOT NULL,            -- JSON array of paths
  symbols_touched TEXT,                   -- JSON array of qualified symbol names, nullable
  summary TEXT NOT NULL,                  -- LLM-generated prose, <= 200 tokens
  summary_model TEXT NOT NULL,            -- e.g. "haiku-3.5", "local-qwen-2.5-7b"
  embedding BLOB,                         -- nullable; same dim as symbol embeddings
  index_version INTEGER NOT NULL
);
CREATE INDEX idx_commit_author_date ON commit_chunks(author_date);
CREATE INDEX idx_commit_files ON commit_chunks(files_touched);  -- LIKE-scanned
```

Reuses the existing embedding dim/ranker so retrieval is one merged query, not two.

## Summariser choice (decide on claim)

Three options, pick one before starting:

| Option | Cost per commit | Latency | Quality | Pick when |
|---|---|---|---|---|
| **Local SLM (Ollama qwen-2.5-7b)** | free | ~2s | acceptable | local-first / privacy stance dominates |
| **Haiku 3.5 / Gemini Flash** | ~$0.0001 | ~600ms | good | speed + cost both matter |
| **Frontier nightly batch** | ~$0.002 | overnight | best | one-time backfill of huge histories |

Default recommendation: **Haiku 3.5 for incremental, local SLM as configurable fallback.** Atelier already has `model_routing` which can express this preference.

## Summary prompt (deterministic, version-pinned)

```
Summarise this commit in 80–120 words. Cover:
1. Primary objective (what problem was solved)
2. Key files and functions changed
3. Technical terminology a future reader would search for

Do not include the commit hash or author. Do not include any code.
Do not editorialise. Plain prose only.

<COMMIT_MESSAGE>
{commit.message}
</COMMIT_MESSAGE>

<DIFF_TRUNCATED_TO_2K_TOKENS>
{diff[:2000_tokens]}
</DIFF_TRUNCATED_TO_2K_TOKENS>
```

The prompt is version-pinned in `summarizer.py` as `_PROMPT_V1`. Bumping the version triggers re-summarisation; lower versions are still searchable until backfill completes.

## Bootstrap & incremental update

- **Bootstrap**: on first `code op="search"` against a repo, schedule a background walk of the last 500 commits via `walker.py`. Summarise + embed. Persist progress; resumable.
- **Incremental**: a post-commit hook (registered via `integrations/claude/plugin/hooks/`) summarises new commits on next session start. Falls back to startup catch-up if the hook is missing.

Limit: skip merge commits with no file-level diff. Skip commits with >50 files touched (likely vendor/codegen) unless their commit message has a manual `[lineage:keep]` tag.

## Query surface

No new MCP tool. Two integration points:

1. **Merged into `code op="search"`** — top-k results include commit chunks ranked by the same embedding score. Each result carries `provenance ∈ {"symbol", "file", "commit"}` and `commit_sha` when applicable.
2. **Filter parameter** — `code op="search" provenance="commit"` returns only commit chunks. Used by the agent when explicitly investigating history.

Default ranking weight: commit chunks get a small score penalty (configurable, default −0.1) so they don't crowd out current-file results unless their summary is much more relevant.

## Validation

Tests under `tests/infra/code_intel/git_history/`:

- `test_summarizer.py` — fake-LLM fixture; assert summary length, no code, no PII leakage.
- `test_embedder.py` — embedding persists to `commit_chunks` table; vector dim matches symbol embeddings.
- `test_search_merge.py` — `engine.search_symbols(...)` returns both symbol and commit results in one ranked list; `provenance` field correct.
- `test_walker_resume.py` — bootstrap walk interrupted mid-way resumes from last persisted commit.

Benchmark under `tests/benchmarks/context_quality/M1_lineage.py`:

- Sample 10 commits from this repo's history that fixed real bugs.
- For each: ask the agent "is there prior art for X in this repo?" where X is the bug.
- Score: agent cites the relevant prior commit (1 point) vs. invents a generic answer (0).
- Target: ≥7/10 with M1 enabled; baseline expected ≤2/10.

## Exit criteria

- Full bootstrap walk completes on the Atelier repo without error and persists to SQLite.
- Incremental update fires on next session start when new commits exist.
- `code op="search"` merges commit chunks with correct provenance tagging.
- Benchmark target hit (≥7/10).
- Trace recorded via `mcp__atelier__trace` referencing this milestone.

## Open questions

- Should `files_touched` be a JSON array (current proposal) or a join table? Join table is cleaner but adds a write path. Decide on profiling.
- Do we summarise commits authored by bots (Dependabot, Renovate)? Default: skip unless `[lineage:keep]` tag present.
- Do we extract `symbols_touched` via SCIP delta or skip on M1 and add in a follow-up? Skipping reduces M1 scope by ~½ day.
