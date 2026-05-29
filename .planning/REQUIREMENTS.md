# Requirements: Atelier Public Benchmarks

**Defined:** 2026-05-28
**Core Value:** A stranger can clone the repo, run one command, and reproduce the exact benchmark results we published — including the losses.

## v1 Requirements

### Bench Mode Toggle

- [ ] **MODE-01**: `ATELIER_BENCH_MODE=off` disables model router (passthrough, no downtiering)
- [ ] **MODE-02**: `ATELIER_BENCH_MODE=off` disables context compactor (passthrough, no LLM hints)
- [ ] **MODE-03**: `ATELIER_BENCH_MODE=off` disables memory adapters (reads return empty)
- [ ] **MODE-04**: `ATELIER_BENCH_MODE=off` disables MCP tool substitution (agent uses native Read/Grep)
- [ ] **MODE-05**: Bench mode bootstrap reads env var once at process start before any module import, tags all telemetry with `bench_mode=on|off`
- [ ] **MODE-06**: Off-arm uses a separate `ATELIER_ROOT` directory to prevent filesystem state leakage between arms
- [ ] **MODE-07**: Unit tests verify router passthrough, compactor passthrough, and MCP tool skipping under off mode
- [ ] **MODE-08**: Integration test shows measurably different token counts between on-arm and off-arm for the same prompt

### TerminalBench Adapter

- [x] **TB-01**: `benchmarks/terminalbench/` package installs via isolated `benchmarks/pyproject.toml` (Python 3.12 workspace) to resolve version conflict
- [x] **TB-02**: `agent_adapter.py` invokes `claude -p --output-format stream-json --verbose` as subprocess, parses the `result` line for `total_cost_usd`, `duration_ms`, `usage.input_tokens`, `usage.output_tokens`, `usage.cache_creation_input_tokens`, `usage.cache_read_input_tokens`
- [x] **TB-03**: `tasks.yaml` pins 10 TerminalBench task IDs that complete in <30 min and exercise code-editing capabilities
- [x] **TB-04**: Runner produces per-run transcript JSON with all required fields populated (transcript, token counts, latency, cost, grader verdict)
- [x] **TB-05**: `--mode on` and `--mode off` arms produce distinguishably different transcripts

### A/B Runner

- [ ] **AB-01**: `benchmarks/ab/runner.py` CLI: `python -m benchmarks.ab.runner --suite terminalbench --tasks 10 --n 5 --models claude-sonnet --modes on,off --out bench/runs/<run-id>/`
- [ ] **AB-02**: Arm executions are interleaved rep-by-rep (not batched all-on then all-off) to equalize prompt cache temperature
- [ ] **AB-03**: Resumable: re-running with same `--run-id` skips cells where `raw/<task>__<mode>__rep<N>.json` already exists (atomic write via `os.replace`)
- [ ] **AB-04**: `--seed 42` propagates deterministic task ordering; same seed produces same ordering across two runs
- [ ] **AB-05**: `summary.json` stores raw `{passed: k, total: n}` per cell (not `p_hat`) with Wilson score 95% CI
- [ ] **AB-06**: Full sweep (10 tasks × 2 modes × 5 reps = 100 runs) completes and produces a valid `summary.json`

### Report Generator

- [ ] **RPT-01**: `benchmarks/ab/report.py` produces 3 delta PNG plots: cost_delta, latency_delta, quality_delta (each with 95% CI error bars)
- [ ] **RPT-02**: `report.md` includes methodology section (model, N, harness, commit SHA, exact CLI command to reproduce)
- [ ] **RPT-03**: `report.md` includes headline table: per-task `Atelier-on | Atelier-off | Δ | 95% CI` for cost, latency, pass-rate
- [ ] **RPT-04**: Every table cell links to its per-run transcript JSON file
- [ ] **RPT-05**: `report.md` includes an explicit Losses section enumerating cells where Atelier-on was slower, costlier, or lower quality — present even when empty ("no losses this run")
- [ ] **RPT-06**: `report.md` renders cleanly on GitHub (no broken MDX, images display correctly)

### Publication Pipeline

- [ ] **PUB-01**: `atelier bench publish <run-id> --out docs-site/blog/<slug>/` assembles a self-contained post directory: `index.md`, `transcripts/`, `plots/`, `reproduce.sh`
- [ ] **PUB-02**: `reproduce.sh` contains the exact CLI command + commit SHA; running it on a fresh clone regenerates the same `summary.json`
- [ ] **PUB-03**: `index.md` has valid Docusaurus frontmatter (title, date, authors, tags) and `<!-- truncate -->` early
- [ ] **PUB-04**: `docs-site/docusaurus.config.ts` enables blog routing (currently `blog: false`)
- [ ] **PUB-05**: Post renders correctly in the existing docusaurus site

### Long-Session Degradation Suite

- [ ] **LS-01**: `benchmarks/ab/suites/long_session.py` defines tasks at 50-turn, 100-turn, and 200-turn cuts requiring multi-step context recall
- [ ] **LS-02**: `benchmarks/ab/graders/recall_rubric.py` grades fact recall and consistency vs turn-1 setup using LLM-as-judge with pinned judge model/version
- [ ] **LS-03**: Runner produces quality-delta by turn-count cut in `summary.json`
- [ ] **LS-04**: Published report includes honest losses if quality degrades at high turn counts

### User-Facing CLI

- [ ] **CLI-01**: `atelier bench run --suite terminalbench --quick` runs 1 task, N=2, both modes, completes in <5 min
- [ ] **CLI-02**: `atelier bench run --suite terminalbench --full` runs 10 tasks, N=5, both modes
- [ ] **CLI-03**: Before spending any tokens, prints cost estimate in $ and requires `--yes` to proceed (or `--no-cost-cap` to override $50 hard-stop)
- [ ] **CLI-04**: Prints live Rich progress during runs and a final terminal comparison table (cost, latency, pass-rate per mode with delta)
- [ ] **CLI-05**: Results stored under `~/.atelier/bench/<run-id>/` for later `atelier bench publish`
- [ ] **CLI-06**: `atelier bench run --help` documents all subcommands

### PR-Replay Benchmarks

- [ ] **PR-01**: `atelier bench run --pr <github-url>` fetches PR metadata (title, body, base commit SHA, real diff)
- [ ] **PR-02**: Runner checks out base commit in a git worktree per arm (on/off), runs agent with PR title+body as prompt
- [ ] **PR-03**: Diff quality scored against the real merged diff using `difflib.SequenceMatcher.ratio()` + hunk coverage (file overlap)
- [ ] **PR-04**: LLM-as-judge rubric provides weighted quality score; judge model is pinned and stated in report (non-Claude judge to avoid bias)
- [ ] **PR-05**: Per-PR comparison table printed: cost, latency, diff similarity score, judge score per arm with delta
- [ ] **PR-06**: Each replay produces a transcript JSON stored under `~/.atelier/bench/<run-id>/`

## v0.2-v0.3 Requirements — Context Quality Lift

### Context Lineage

- [x] **LINEAGE-01**: Bootstrap walk summarises last 500 commits and persists to `commit_chunks` SQLite table; merge commits and commits with >50 files touched are skipped automatically
- [x] **LINEAGE-02**: Incremental update fires on next session start when new commits exist; walk is resumable if interrupted mid-way
- [x] **LINEAGE-03**: `code op="search"` merges commit chunks with symbol/file results; each commit result carries `provenance="commit"` and `commit_sha` fields
- [x] **LINEAGE-04**: `code op="search" provenance="commit"` filter returns only commit chunk results
- [x] **LINEAGE-05**: Summariser uses version-pinned prompt (`_PROMPT_V1`); bumping the version triggers re-summarisation of all commits
- [x] **LINEAGE-06**: Commit chunks get a small score penalty (configurable, default −0.1) so they don't crowd current-file results

### Cache-Aware Routing

- [x] **CACHE-01**: `ModelRouter.recommend()` accepts optional `prior_plan`, `current_plan`, `prior_route`, `stickiness_remaining` arguments; existing callers compile without change
- [x] **CACHE-02**: Router stays on prior model when `cache_eviction_cost_usd > estimated_quality_gain_usd`
- [x] **CACHE-03**: Routes are sticky for a configurable window (default 3 follow-up tool calls) within a single agent turn; stickiness resets on new user-visible response
- [x] **CACHE-04**: Every `recommend()` call emits a `route_decision` event to the run ledger with `cache_cost_usd`, `quality_gain_usd_estimated`, `decision`, and `stickiness_remaining` fields
- [x] **CACHE-05**: New `cache_cost.py` pure function `cache_eviction_cost_usd(plan_a, plan_b, pricing)` and `stickiness.py` turn-window state module added

### Counterexample Loop

- [ ] **COUNTER-01**: `VerifierCapability` runs lint/typecheck/tests/semantic checks scoped to files touched by the agent in the current attempt
- [ ] **COUNTER-02**: Each failure produces a structured `Counterexample` dataclass with `check`, `severity`, `file_path`, `line`, `diagnostic`, `expected`, `actual`, `repro_command` fields
- [ ] **COUNTER-03**: Counterexamples injected as **tool-result channel** blocks (never system prompt); prompt compiler rejects `Counterexample` blocks with Stability ≥ BRANCH
- [ ] **COUNTER-04**: Retry loop capped at 3 attempts per subtask; budget exhaustion calls `rescue.invoke(reason="verification_budget_exhausted")`
- [ ] **COUNTER-05**: Test scoping: only tests whose paths match touched files run inside the loop; full-suite run is never triggered automatically

### Phase-Linear Cache Reuse

- [x] **LINEAR-01**: `context_reuse` defines `Phase`, `PhasePlan`, `PhaseResult`, cache-stat fields, and a declarative Survey → Plan → Implement phase state machine
- [x] **LINEAR-02**: Survey and Plan run under one fixed system prompt with per-phase user objectives; Plan uses `continue_from="survey"` so provider cache can read the Survey prefix warm
- [x] **LINEAR-03**: Read-only Survey/Plan tool profile uses safe source minification and records original vs minified token counts; writer profile reads exact bytes
- [x] **LINEAR-04**: Runtime exposes `linear | per_agent | auto` mode selection with an auto heuristic that avoids linear mode for divergent or oversized contexts
- [x] **LINEAR-05**: Linear-vs-per-agent benchmark proves ≥30% lower cost and ≥25% lower wall-time at equal-or-better success on context-sharing scenarios

### Scoped Pull Context

- [ ] **SCOPED-01**: `ScopedContextCapability.pull(subtask: Subtask) → ScopedContext` returns ranked, budget-packed chunks within `subtask.budget_tokens` (default 4000)
- [ ] **SCOPED-02**: Chunks from `subtask.excluded_paths` are never included in output
- [ ] **SCOPED-03**: `ScopedContext` includes `rationale` (citing top candidate scores), `excluded` (every dropped candidate with reason), and `trace_id` fields
- [ ] **SCOPED-04**: Results are cached by `hash(subtask.description + affected_paths + keywords + index_version)`; second call with identical `Subtask` returns cached result with `provenance="cached"`
- [ ] **SCOPED-05**: `context op="pull"` MCP op registered; accepts `subtask`, `budget_tokens`, `affected_paths`, `excluded_paths` parameters
- [ ] **SCOPED-06**: M1 commit chunks surface in scoped pull results when subtask description matches prior commit summaries

### Cross-Milestone Evaluation

- [x] **CQEVAL-01**: `tests/benchmarks/context_quality/` suite exists with benchmark modules for M1–M4 and a README describing the internal eval protocol
- [x] **CQEVAL-02**: M1 benchmark (`M1_lineage.py`): ≥7/10 commit history queries answered correctly (baseline ≤2/10 expected)
- [x] **CQEVAL-03**: M2 benchmark (`M2_routing.py`): ≥10% cost reduction on 50 replayed session traces with no quality-tier regressions
- [ ] **CQEVAL-04**: M3 benchmark (`M3_verification.py`): ≥60% self-correction rate on 20 seeded type-error edits (baseline ≤15% expected)
- [ ] **CQEVAL-05**: M4 benchmark (`M4_scoped.py`): precision ≥0.6 and recall ≥0.85 on 20 multi-file edits from this repo's history

### Local Benchmark Proof

- [x] **TBEVAL-01**: Local linear-vs-per-agent benchmark artifact records cost, latency, cache-hit ratio, minification delta, and task success for at least 7 representative scenarios
- [ ] **TBEVAL-02**: TerminalBench-oriented local proof run shows Atelier-on target ≥90% pass rate while cheaper and faster than the off/per-agent baseline, or records concrete implementation gaps and loops back before finalizing

## v0.4 Requirements — Dedicated Language Support

### Language Registry

- [x] **DLS-LANG-01**: A canonical language registry exists as the single source of truth for recognized language identity, extensions, parser names, and SCIP indexer metadata
- [x] **DLS-LANG-02**: Extension-based language detection delegates to the registry while preserving the `"text"` fallback for unknown files
- [x] **DLS-LANG-03**: Shell extensions (`.sh`, `.bash`, `.zsh`) resolve to the canonical tree-sitter-compatible bash language key
- [x] **DLS-LANG-04**: Tree-sitter config keys, repo-map tag language detection, and SCIP binary registry keys all use canonical language names

### Tree-sitter Outline Coverage

- [x] **DLS-OUTLINE-01**: Shell/bash files produce dedicated tree-sitter outlines with meaningful function and assignment structure
- [x] **DLS-OUTLINE-02**: SQL files produce dedicated tree-sitter outlines for schema-level constructs such as tables, views, functions, and indexes
- [x] **DLS-OUTLINE-03**: YAML files produce dedicated tree-sitter outlines for top-level document structure
- [x] **DLS-OUTLINE-04**: TOML files produce dedicated tree-sitter outlines for table headers and top-level key/value structure
- [x] **DLS-OUTLINE-05**: JSON files produce dedicated tree-sitter outlines for top-level object structure when parser availability and the 25% savings threshold justify it

### Tree-sitter Repo-map Tags

- [x] **DLS-TAGS-01**: Repo-map tag extraction uses tree-sitter-derived tags for all tree-sitter-supported languages
- [x] **DLS-TAGS-02**: Python keeps its existing AST-based tag extraction path
- [x] **DLS-TAGS-03**: Unknown or unsupported languages keep regex tag extraction as a fallback instead of failing
- [x] **DLS-TAGS-04**: Repo-map/PageRank can consume tags from languages that previously produced no useful symbols

### SCIP Registry and Lazy Indexing

- [x] **DLS-SCIP-01**: SCIP language registry coverage expands to Go, Rust, Java, Ruby, C, and C++ in addition to Python, TypeScript, and JavaScript
- [x] **DLS-SCIP-02**: SCIP registry entries support per-language env overrides, fallback commands, and argv templates including subcommand-style invocations
- [x] **DLS-SCIP-03**: Lazy or opt-in SCIP indexer execution writes `.scip` artifacts into the repo-local cache
- [x] **DLS-SCIP-04**: Java and C/C++ indexers skip cleanly when required build context is unavailable

### SCIP Runtime Provisioning

- [x] **DLS-PROV-01**: `scip-python` and `scip-typescript` install into Atelier-managed runtime directories at install time
- [x] **DLS-PROV-02**: SCIP binary discovery searches Atelier-managed binary directories before system PATH
- [x] **DLS-PROV-03**: Tier-2 SCIP indexers can be lazily fetched with checksum verification and safe offline failure
- [x] **DLS-PROV-04**: Tier-3 toolchain-backed indexers such as Rust and Java are detected and documented without auto-installing heavy toolchains
- [x] **DLS-PROV-05**: Runtime status or availability output shows which SCIP languages are ready, missing, or require user-provided toolchains

### Validation, Benchmarks, and Docs

- [x] **DLS-VAL-01**: A per-language fixture matrix validates detection, expected outline kind, and tag behavior
- [x] **DLS-VAL-02**: Honest savings benchmarks compare newly dedicated outline languages against generic and full-file paths
- [x] **DLS-VAL-03**: A SCIP availability report matches the expanded registry and provisioning matrix
- [x] **DLS-VAL-04**: Language-support, architecture, installation, quick-reference, and SCIP provisioning docs reflect actual shipped behavior

## v2 Requirements

### Enhanced PR-Replay

- **PR-V2-01**: Optional test-suite execution after agent run (grades by test pass-rate, not just diff quality)
- **PR-V2-02**: Multi-PR batch mode: `--pr-list <file>` runs a set of PRs as a mini-suite

### SWE-Bench Integration

- **SWE-01**: `benchmarks/swe/` updated with explicit Atelier-off control arm matching D1 bench-mode toggle
- **SWE-02**: SWE-bench results publishable via same D5 external publication pipeline

### Automated Leaderboard

- **LB-01**: Periodic CI run (weekly) produces a benchmark snapshot and auto-publishes to docs-site
- **LB-02**: Leaderboard page at `docs-site/benchmarks/` aggregating all published runs

## Out of Scope

| Feature | Reason |
|---------|--------|
| Cost-only benchmarks (no quality) | Violates non-negotiable rule #1; misleading without quality signal |
| Using `benchmarks/benchmarking.py` numbers | Hardcodes fictional constants (saved_per_lesson_in=350); publishing would be fraudulent |
| tiktoken for token counting in published numbers | 10-30% systematic error vs Claude; must use API `usage` field exclusively |
| Normal approximation CI for pass-rate | Mathematically wrong at N=5; Wilson score is required |
| Vendoring TerminalBench source | Must be pinned submodule or PyPI dep for reproducibility |
| Hiding losses | Every report must include a losses section even when Atelier-on wins all cells |
| Using Claude as judge for PR-replay | Bias risk (judging its own host); use GPT-4o or Gemini as judge |

## Traceability

### v0.1 (Public Benchmarks MVP)

| Requirement | Phase | Status |
|-------------|-------|--------|
| MODE-01–08 | Phase 1 | Pending |
| TB-01–05 | Phase 2 | Pending |
| AB-01–06 | Phase 3 | Pending |
| RPT-01–06 | Phase 4 | Pending |
| PUB-01–05 | Phase 5 | Pending |
| CLI-01–06 | Phase 6 | Pending |
| LS-01–04 | Phase 6 | Pending |
| PR-01–06 | Phase 7 | Pending |

### v0.2 (Context Lineage)

| Requirement | Phase | Status |
|-------------|-------|--------|
| LINEAGE-01 | Phase 8 | Complete |
| LINEAGE-02 | Phase 8 | Complete |
| LINEAGE-03 | Phase 8 | Complete |
| LINEAGE-04 | Phase 8 | Complete |
| LINEAGE-05 | Phase 8 | Complete |
| LINEAGE-06 | Phase 8 | Complete |
| CQEVAL-01 | Phase 8 | Complete |
| CQEVAL-02 | Phase 8 | Complete |

### v0.3 (Context Quality Execution)

| Requirement | Phase | Status |
|-------------|-------|--------|
| CACHE-01 | Phase 12 | Complete |
| CACHE-02 | Phase 12 | Complete |
| CACHE-03 | Phase 12 | Complete |
| CACHE-04 | Phase 12 | Complete |
| CACHE-05 | Phase 12 | Complete |
| CQEVAL-03 | Phase 12 | Complete |
| LINEAR-01 | Phase 13 | Complete |
| LINEAR-02 | Phase 13 | Complete |
| LINEAR-03 | Phase 13 | Complete |
| LINEAR-04 | Phase 13 | Complete |
| LINEAR-05 | Phase 13 | Complete |
| TBEVAL-01 | Phase 13 | Complete |
| COUNTER-01 | Phase 14 | Pending |
| COUNTER-02 | Phase 14 | Pending |
| COUNTER-03 | Phase 14 | Pending |
| COUNTER-04 | Phase 14 | Pending |
| COUNTER-05 | Phase 14 | Pending |
| CQEVAL-04 | Phase 14 | Pending |
| SCOPED-01 | Phase 15 | Pending |
| SCOPED-02 | Phase 15 | Pending |
| SCOPED-03 | Phase 15 | Pending |
| SCOPED-04 | Phase 15 | Pending |
| SCOPED-05 | Phase 15 | Pending |
| SCOPED-06 | Phase 15 | Pending |
| CQEVAL-05 | Phase 15 | Pending |
| TBEVAL-02 | Phase 15 | Pending |

### v0.4 (Dedicated Language Support)

| Requirement | Phase | Status |
|-------------|-------|--------|
| DLS-LANG-01 | Phase 16 | Complete |
| DLS-LANG-02 | Phase 16 | Complete |
| DLS-LANG-03 | Phase 16 | Complete |
| DLS-LANG-04 | Phase 16 | Complete |
| DLS-OUTLINE-01 | Phase 17 | Complete |
| DLS-OUTLINE-02 | Phase 17 | Complete |
| DLS-OUTLINE-03 | Phase 17 | Complete |
| DLS-OUTLINE-04 | Phase 17 | Complete |
| DLS-OUTLINE-05 | Phase 17 | Complete |
| DLS-TAGS-01 | Phase 18 | Complete |
| DLS-TAGS-02 | Phase 18 | Complete |
| DLS-TAGS-03 | Phase 18 | Complete |
| DLS-TAGS-04 | Phase 18 | Complete |
| DLS-SCIP-01 | Phase 19 | Complete |
| DLS-SCIP-02 | Phase 19 | Complete |
| DLS-SCIP-03 | Phase 19 | Complete |
| DLS-SCIP-04 | Phase 19 | Complete |
| DLS-PROV-01 | Phase 20 | Complete |
| DLS-PROV-02 | Phase 20 | Complete |
| DLS-PROV-03 | Phase 20 | Complete |
| DLS-PROV-04 | Phase 20 | Complete |
| DLS-PROV-05 | Phase 20 | Complete |
| DLS-VAL-01 | Phase 21 | Complete |
| DLS-VAL-02 | Phase 21 | Complete |
| DLS-VAL-03 | Phase 21 | Complete |
| DLS-VAL-04 | Phase 21 | Complete |

**Coverage:**
- v0.1 requirements: 47 total | Mapped: 47 | Unmapped: 0 ✓
- v0.2 requirements: 8 total | Mapped: 8 | Unmapped: 0 ✓
- v0.3 requirements: 26 total | Mapped: 26 | Unmapped: 0 ✓
- v0.4 requirements: 26 total | Mapped: 26 | Unmapped: 0 ✓

---
*Requirements defined: 2026-05-28*
*Last updated: 2026-05-29 — Phase 19 expanded SCIP registry requirements complete*
