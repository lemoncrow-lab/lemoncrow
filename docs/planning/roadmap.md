# LemonCrow Roadmap

> LemonCrow has no committed release schedule. The current product priority is
> **human-attention compression in software review**: reduce the amount of human
> attention required to understand each change without outsourcing engineering
> judgment. Older runtime, context, and cost work remains useful substrate, but
> should not outrank this review-first direction without user evidence.

The sections below separate shipped capabilities from ideas that may or may not
be pursued on a best-effort basis. Nothing here is a commitment.

For implementation-level status, evidence, acceptance gates, and sequencing of
the six cost levers, see the
[savings optimization status and roadmap](./savings-optimization-roadmap.md).
For the September 2026 product plan — narrowed for LemonCrow's current low-user,
fully-open-source stage around human review intelligence, AI usage visibility, and
bring-your-own/self-hosted models — see the
[LemonCrow product and execution plan](./2026-09-07-agent-runtime-competitive-plan.md).

For the review-first product design — including the attention-compression framework,
priority list, Review Chapters, unified annotations, revision-aware feedback loop,
and later peer-review/inbox work — see the
[LemonCrow Review product, UX, and execution plan](./2026-09-08-review-first-developer-workspace.md).

## Shipped capabilities

### Human review, AI usage, and bring-your-own models

PR-1 through PR-12 of the
[September 2026 product and execution plan](./2026-09-07-agent-runtime-competitive-plan.md)
have landed on the `feat/review-usage-model` branch. This is a status record, not
a commitment to anything beyond it.

| PR    | Shipped                                                                                  |
| ----- | ---------------------------------------------------------------------------------------- |
| PR-1  | Review packet model and raw diff integration — the `lc review` core                     |
| PR-2  | Semantic change impact: untouched call sites, contract literals, signature changes       |
| PR-3  | Human-oriented review ordering — review risk first, not alphabetical                    |
| PR-4  | Agent provenance in review packets — which session produced the change                  |
| PR-5  | Local, self-contained review HTML view (`lc review --html` / `--open`)                   |
| PR-6  | Canonical usage read model — one row shape across hosts, models, and projects           |
| PR-7  | `lc usage` with `explain`, `optimize`, and `rows`; `lc savings` kept as a hidden alias    |
| PR-8  | Generic self-hosted endpoint setup (`lc model add`)                                      |
| PR-9  | Model capability probe (`lc model probe`, `lc model list`)                                |
| PR-10 | Minimal `lc run explain` — run attribution where every line cites its record            |
| PR-11 | `lc context doctor` — context loaded versus context actually used                       |
| PR-12 | Lightweight `lc resume-context`                                                          |

These surfaces are deterministic and LLM-free: no model is called and nothing is
generated. Three limits are structural rather than incidental, and the shipped
output states each one instead of papering over it:

- **Session ↔ commit correlation is a heuristic, not a join.** No session record
  written before the run ledger's `git` anchor stores a commit sha, branch, or
  worktree id, so `lc review` scores candidates on workspace path and time
  window. It always prints the evidence that fired, falls back to `unknown`
  rather than guessing, and accepts `--session-id` as a deterministic override.
  New runs stamp HEAD into `run.json`, which makes future correlation exact and
  does nothing for sessions already on disk.
- **Files-read capture is Claude-only.** Every other host importer records edits
  but not reads, so the honest answer there is `Agent inspected: not recorded for
  this host` — never `0 files`, which would read as a finding rather than a gap.
  The "not inspected by the generating agent" ordering signal is applied only
  where read capture actually exists, so a host without it never inflates every
  file's rank.
- **Test-execution evidence is thin.** Status is derived only from recorded
  command exit codes. A record carrying no exit code is `UNKNOWN`, the migration
  check is always `UNKNOWN`, no result is ever synthesized as `PASS`, and every
  packet ends with `Human review REQUIRED`.

Two further accepted gaps: change impact degrades to a named `degraded` list when
the code index is absent or stale (cross-language reach is text/AST-level only,
not graph-level), and local or self-hosted models with no rate card are reported
as unpriced — `local` or `—`, never `$0.00`.

### Context & memory

- Dynamic context compaction with LLM hints (task type, risk level, must-keep)
- Sleeptime summarization and deduplication
- Persistent memory store (SQLite/PostgreSQL) with archival recall
- Cross-vendor memory adapters (Claude, Codex)
- Memory arbitration with staleness detection
- Symbol-based memory recall

### Cost tracking

- Per-session cost reports with actual vs counterfactual costs
- `lc usage` (with `explain` / `optimize` / `rows`) and `lc dashboard` commands
- Aggregate cost and token savings with reset support
- Counterfactual pricing engine

### Code intelligence

- Symbol-first code index with multi-language support
- AST pattern matching (ast-grep) with rewrite support
- LemonGraph — call graph (callers/callees) with centrality scoring
- Usages and reference resolution
- Cross-language edge resolution (ctypes, subprocess, dynamic import)
- Git history analysis (blame, graveyard, renames, walker)
- Zoekt backend for large repos
- Repo-map with PageRank

### CLI & service surface

- MCP server (local and remote modes)
- OpenAI-compatible `/v1/chat/completions` gateway
- Runtime commands: runs, ledger, swarm, lessons, benchmarks
- Outcome capture for agent-run evaluation
- `lc insights` weekly summary with spend trends and opportunities
- Lesson promotion with PR bot
- Live reviewer agent
- Background services with auto-update

### Host integrations

- Claude Code, Codex, Copilot, OpenCode, LemonCode, Cursor, Antigravity, Hermes
- SDK adapters (Anthropic tools, OpenAI SDK hooks, LangChain middleware)

### Storage & telemetry

- SQLite and PostgreSQL storage backends
- pgvector for embedding similarity search
- OpenTelemetry → PostHog + GCP, local-first, anonymous

## Product direction

LemonCrow is not building a general-purpose organizational knowledge warehouse. External systems such as Jira, Confluence, GitHub, and repositories remain sources of truth. The direction is the execution last mile:

\`code + ticket + docs → task working set → agent run → tests/review → source-linked memory\`

Future team context must be reviewable, permission-aware, tied to source and commit provenance, checked for staleness, and promoted from verified outcomes rather than raw transcripts. These are direction statements, not shipped-feature claims.

## Possible directions (non-promises)

These are directions that may be explored on a best-effort basis. They are not
commitments, dated deliverables, or shipped features.

| Area                  | Description                                                                              |
| --------------------- | ---------------------------------------------------------------------------------------- |
| Model routing         | Provider/model evaluation and reliability work                                          |
| Optimization advisor  | `lc optimize` with compaction type taxonomy, golden tests, policy presets, shadow runner |
| Cross-machine sync    | Encrypted workspace sync across machines                                                 |
| Web dashboard         | Browser-based spend trends and management                                                |
| Benchmark publication | One-command export to publishable JSON + markdown                                        |
| Local agentic search  | Small local model runs the explore/search loop; only exact matched spans are passed up to the frontier model |
| Multi-turn retrieval  | Iterative retrieval that returns the exact document the model asked for instead of top-k chunks |
| LemonGraph viewer     | Hosted, browser-based visualization of the local call/knowledge graph                            |

## Not planned

- Custom models, fine-tuning, or in-house embeddings
- IDE plugins
- Mobile companion
