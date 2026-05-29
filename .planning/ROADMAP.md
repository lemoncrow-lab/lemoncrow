# Roadmap: Atelier Public Benchmarks

**Milestone:** v0.1 — Public Benchmarks MVP
**Core Value:** A stranger can clone the repo, run one command, and reproduce the exact benchmark results we published — including the losses.
**Created:** 2026-05-28
**Critical path:** Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 (first published report by 2026-06-04)

---

## Phases

- [x] **Phase 1: Bench-Mode Toggle** — Clean `ATELIER_BENCH_MODE` on/off toggle that gates router, compactor, memory, and MCP tools; unblocks every downstream phase
- [x] **Phase 2: TerminalBench Adapter** — Isolated Python 3.12 workspace running TerminalBench tasks via `claude -p` subprocess, capturing full transcript + metrics per run (completed 2026-05-28)
- [x] **Phase 3: A/B Runner** — Interleaved N-rep execution with seeded determinism, resumability, and Wilson-score `summary.json`
- [x] **Phase 4: Report Generator** — Three delta plots (cost/latency/quality) and a `report.md` with methodology, headline table, transcript links, and explicit losses section
- [x] **Phase 5: Publication Pipeline** — `atelier bench publish` assembles a self-contained Docusaurus blog post; fixes `blog: false` in docusaurus.config.ts
- [x] **Phase 6: Long-Session Suite + User-Facing CLI** — Recall-rubric grader for 50/100/200-turn degradation, plus `atelier bench run --quick/--full` with Rich progress and cost gate
- [x] **Phase 7: PR-Replay Benchmarks** — `atelier bench run --pr <url>` replays any GitHub PR A/B with diff-quality scoring; non-Claude judge to avoid self-judging bias

---

## Phase Details

### Phase 1: Bench-Mode Toggle
**Goal**: `ATELIER_BENCH_MODE=off` produces a clean, verifiable baseline arm — no Atelier routing, compaction, memory reads, or MCP tool substitution — without polluting production code paths.
**Depends on**: Nothing (foundational blocker for all other phases)
**Requirements**: MODE-01, MODE-02, MODE-03, MODE-04, MODE-05, MODE-06, MODE-07, MODE-08

**Tasks**:
- Create `src/atelier/bench/mode.py` with `BenchMode` enum, `bootstrap()` singleton (read once at process start), `is_off()` predicate
- Add passthrough guard in `src/atelier/core/capabilities/cross_vendor_routing/router.py`: `if bench.is_off(): return PassthroughRoute(requested_model)`
- Add passthrough guard in context compaction capability: `if bench.is_off(): return CompressionResult.passthrough(ledger)`
- Disable memory adapter reads in all cross-vendor-memory adapters: `if bench.is_off(): return []`
- Gate MCP tool registration in `mcp_server.py` at module top (read mode before `@mcp_tool` decorators fire); ensure `ATELIER_DEV_MODE` cannot override bench-mode gating
- Call `bench.bootstrap()` as first line in `src/atelier/gateway/cli/app.py` `main()` and tag all telemetry with `bench_mode=on|off`
- Enforce separate `ATELIER_ROOT` per arm: each bench arm receives a fresh temp directory via `ATELIER_ROOT` env var; document cross-replication cleanup
- Write unit tests: `test_bench_mode_off_disables_router`, `test_bench_mode_off_disables_compactor`, `test_bench_mode_off_disables_mcp_tools`
- Write integration test: same prompt under `mode=on` and `mode=off` produces measurably different token counts in telemetry

**Acceptance criteria**:
1. `ATELIER_BENCH_MODE=off atelier --version` runs without invoking the model router or context compactor (confirmed by debug log / unit assertion)
2. `ATELIER_BENCH_MODE=off` arm returns empty list from every memory adapter read; `ATELIER_BENCH_MODE=on` arm returns non-empty for a seeded session
3. Unit tests for router passthrough, compactor passthrough, and MCP tool gating all pass
4. Integration test shows measurably different token counts between on-arm and off-arm for the same prompt
5. Running the off-arm twice in sequence leaves `cost_history.json` clean (no on-arm entries leak across runs)

**Dependencies**: None
**Plans**: 3 plans

Plans:
- [ ] 01-01-PLAN.md — bench package (mode.py singleton + make_arm_env) + environment.py bench-first gate + CLI bootstrap in main()
- [ ] 01-02-PLAN.md — capability guards: CrossVendorRouter, ModelRouter, ContextCompressionCapability, MemoryRegistry
- [ ] 01-03-PLAN.md — unit tests (MODE-07, MODE-06 isolation API) + slow integration test (MODE-08)

---

### Phase 2: TerminalBench Adapter
**Goal**: The benchmark runner can execute any of 10 pinned TerminalBench tasks under both bench modes and receive a fully-populated transcript JSON (tokens, cost, latency, grader verdict) per run — without version conflicts contaminating the main project.
**Depends on**: Phase 1 (bench-mode toggle must be functional)
**Requirements**: TB-01, TB-02, TB-03, TB-04, TB-05

**Tasks**:
- Create isolated `benchmarks/pyproject.toml` (Python 3.12 uv workspace) that declares TerminalBench as a pinned dependency (submodule or PyPI); keep TerminalBench version separate from root `pyproject.toml` to avoid version conflicts
- Create `benchmarks/terminalbench/__init__.py`, `runner.py`, `agent_adapter.py`
- `agent_adapter.py`: invoke `claude -p --output-format stream-json --verbose` as subprocess; parse the `result` line for `total_cost_usd`, `duration_ms`, `usage.input_tokens`, `usage.output_tokens`, `usage.cache_creation_input_tokens`, `usage.cache_read_input_tokens`
- `tasks.yaml`: pin 10 TerminalBench task IDs that complete in <30 min and exercise code-editing capabilities
- Ensure each arm run uses a fresh `ATELIER_ROOT` (per Phase 1 Mode-06 requirement) — wire through subprocess env vars
- Verify `--mode on` and `--mode off` produce distinguishably different transcripts (different tool-call sequences or token counts)

**Acceptance criteria**:
1. `python -m benchmarks.terminalbench.runner --task <task-id> --mode off --model claude-sonnet-4-5` produces a transcript JSON with all required fields populated: `transcript`, `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `latency_ms`, `cost_usd`, `grader_verdict`
2. Same command with `--mode on` produces a distinguishably different transcript (different token counts or tool calls)
3. Full 10-task subset completes in <30 min on a single machine
4. TerminalBench is installed from a pinned submodule or PyPI dep — no vendored source copy in the repo
5. `benchmarks/pyproject.toml` installs cleanly in an isolated Python 3.12 venv without conflicting with root `pyproject.toml` dependencies

**Dependencies**: Phase 1
**Plans**: TBD

---

### Phase 3: A/B Runner
**Goal**: Running `python -m benchmarks.ab.runner` executes N≥5 interleaved replications per cell (on/off × tasks), is resumable on interruption, and produces a `summary.json` with Wilson-score 95% CI per cell.
**Depends on**: Phase 1, Phase 2
**Requirements**: AB-01, AB-02, AB-03, AB-04, AB-05, AB-06

**Tasks**:
- Create `benchmarks/ab/__init__.py`, `runner.py`, `aggregate.py`
- Implement CLI: `python -m benchmarks.ab.runner --suite terminalbench --tasks 10 --n 5 --models claude-sonnet --modes on,off --out bench/runs/<run-id>/`
- **Interleaving (critical)**: schedule executions rep-by-rep — rep 1 of all (task, mode) pairs, then rep 2, etc. — never batch all-on then all-off; this equalizes prompt-cache temperature across arms
- Implement resumability: atomic write via `os.replace` to `raw/<task>__<mode>__rep<N>.json`; re-running with same `--run-id` skips cells where the file already exists
- Implement `--seed 42` deterministic task ordering: same seed → same permutation across independent runs
- `aggregate.py`: compute Wilson score 95% CI for pass-rate (binary metric); store raw `{passed: k, total: n}` per cell in `summary.json` — never store `p_hat` directly
- Output layout: `bench/runs/<run-id>/{config.json, raw/<task>__<mode>__rep<N>.json, summary.json}`
- Use API `usage` field for all token counts — never tiktoken estimates (10–30% systematic error)

**Acceptance criteria**:
1. `python -m benchmarks.ab.runner --suite terminalbench --tasks 10 --n 5 --models claude-sonnet --modes on,off --out bench/runs/test-01/` completes and produces a `summary.json` with all 20 cells (10 tasks × 2 modes) populated
2. Killing the runner mid-sweep and rerunning with the same `--run-id` resumes from the last completed cell without re-running finished cells (verify by comparing checksums of completed raw files)
3. Same `--seed 42` produces identical task ordering across two independent runs (determinism test)
4. `summary.json` stores raw `{passed: k, total: n}` counts and Wilson score 95% CI — no normal approximation (confirm via unit test on known values)
5. Execution log confirms arm executions are interleaved rep-by-rep (not batched all-on then all-off)

**Dependencies**: Phase 1, Phase 2
**Plans**: TBD

---

### Phase 4: Report Generator
**Goal**: `python -m benchmarks.ab.report <run-id>` produces three publication-ready delta plots and a `report.md` that renders cleanly on GitHub, includes a headline table with per-task transcript links, and always shows an explicit losses section.
**Depends on**: Phase 3
**Requirements**: RPT-01, RPT-02, RPT-03, RPT-04, RPT-05, RPT-06

**Tasks**:
- Create `benchmarks/ab/report.py` and `benchmarks/ab/templates/report.md.j2`
- Generate 3 delta PNG plots using matplotlib: `cost_delta.png`, `latency_delta.png`, `quality_delta.png` — each with 95% CI error bars derived from `summary.json`
- Render `report.md` from Jinja2 template with:
  - Methodology section: model, N, harness version, commit SHA, exact CLI command to reproduce
  - Headline table: per-task `Atelier-on | Atelier-off | Δ | 95% CI` for cost, latency, pass-rate
  - Embedded plot images (relative paths that work on GitHub)
  - Per-task transcript links pointing to `raw/<task>__<mode>__rep<N>.json`
  - Explicit **Losses** section enumerating cells where Atelier-on was slower, costlier, or lower quality; section present even when empty ("no losses this run")
- Validate Markdown renders cleanly: no broken MDX, no raw HTML that GitHub won't render, image paths relative

**Acceptance criteria**:
1. `python -m benchmarks.ab.report <run-id>` produces exactly 3 PNG files under `bench/runs/<run-id>/plots/` and a `report.md`
2. The Losses section appears in every generated `report.md` — even for a sweep where Atelier-on wins all cells (row reads "no losses this run")
3. Every cell in the headline table links to an existing `raw/<task>__<mode>__rep<N>.json` file (no broken links)
4. `report.md` renders cleanly on GitHub (verify: push draft, confirm images and tables display correctly, no raw MDX errors)
5. Methodology section includes model name, N value, harness name + version, commit SHA, and the exact CLI command used for the sweep

**Dependencies**: Phase 3
**Plans**: TBD

---

### Phase 5: Publication Pipeline
**Goal**: `atelier bench publish <run-id>` assembles a self-contained Docusaurus blog post directory, the Docusaurus site has blog routing enabled, and the first benchmark post renders correctly at `docs-site/blog/`.
**Depends on**: Phase 4
**Requirements**: PUB-01, PUB-02, PUB-03, PUB-04, PUB-05

**Tasks**:
- Fix `docs-site/docusaurus.config.ts`: change `blog: false` to `blog: { ... }` with appropriate settings to enable blog routing
- Create `src/atelier/infra/benchmarks/external_publisher.py` (mirrors `publisher.py` structure but external-shaped)
- Add `publish` subcommand to `src/atelier/cli/commands/bench.py`: `atelier bench publish <run-id> --out docs-site/blog/<slug>/`
- Assembler logic: copy `report.md` → `index.md` (with intro/conclusion sections added), copy `raw/*.json` → `transcripts/`, copy `plots/*.png` → `plots/`, generate `reproduce.sh`
- `reproduce.sh`: embed exact CLI command + commit SHA so a stranger can run it on a fresh clone and regenerate the same `summary.json`
- `index.md` frontmatter: valid Docusaurus fields (`title`, `date`, `authors`, `tags: [benchmark, atelier-vs-baseline, <model>]`) and `<!-- truncate -->` marker early in the post
- Smoke-test: run `npm run build` or `bun run build` in `docs-site/` to confirm post renders without errors

**Acceptance criteria**:
1. `atelier bench publish <run-id> --out docs-site/blog/2026-06-04-terminalbench-claude-sonnet/` creates a directory containing `index.md`, `transcripts/` (with raw JSON files), `plots/` (with PNG files), and `reproduce.sh`
2. `reproduce.sh`, executed on a fresh clone with valid API keys, regenerates a `summary.json` that matches the original within expected non-deterministic variance
3. `index.md` has valid Docusaurus frontmatter with title, date, authors, and tags; `<!-- truncate -->` appears within the first 20 lines
4. `docs-site/docusaurus.config.ts` has blog routing enabled (not `blog: false`); `bun run build` in `docs-site/` completes without errors
5. The published post renders correctly in the local Docusaurus dev server (`bun run start` in `docs-site/`) — blog listing page shows the post, post page shows plots and tables

**Dependencies**: Phase 4
**Plans**: TBD
**UI hint**: yes

---

### Phase 6: Long-Session Suite + User-Facing CLI
**Goal**: Developers can answer "does Atelier lose context?" with data from the long-session suite, and can trigger any benchmark with a single `atelier bench run` command that shows live progress, requires cost confirmation, and prints a terminal comparison table.
**Depends on**: Phase 3 (runner infrastructure), Phase 5 (publish pipeline for reporting losses)
**Requirements**: LS-01, LS-02, LS-03, LS-04, CLI-01, CLI-02, CLI-03, CLI-04, CLI-05, CLI-06

**Tasks**:

*Long-session suite:*
- Create `benchmarks/ab/suites/long_session.py` defining tasks at 50-turn, 100-turn, and 200-turn cuts requiring multi-step context recall across the session
- Create `benchmarks/ab/suites/long_session_tasks.yaml` with task definitions
- Create `benchmarks/ab/graders/recall_rubric.py`: LLM-as-judge grader with pinned judge model and version; rubric scores fact recall and consistency vs turn-1 setup; pin to a non-Atelier judge (e.g., GPT-4o or Gemini) to avoid self-serving bias
- Wire `--suite long_session` into the A/B runner (Phase 3); `summary.json` includes quality-delta keyed by turn-count cut

*User-facing CLI:*
- Add `run` subcommand to `src/atelier/cli/commands/bench.py`
- `--quick` mode: 1 task, N=2, both modes, target <5 min wall clock
- `--full` mode: 10 tasks, N=5, both modes (matches published config)
- Pre-run cost estimate: compute token estimate, print `"Estimated cost: ~$X.XX — proceed? [y/N]"`, require `--yes` to skip prompt; hard-stop if estimated cost >$50 unless `--no-cost-cap` is passed
- Live Rich progress display during runs (task + mode + rep progress bar, current cost accumulator)
- Final terminal comparison table on completion: cost, latency, pass-rate per mode with Δ
- Store results under `~/.atelier/bench/<run-id>/` for later `atelier bench publish`
- `atelier bench run --help` documents all subcommands and flags

**Acceptance criteria**:
1. `python -m benchmarks.ab.runner --suite long_session --n 5` produces a `summary.json` with quality-delta broken down by turn-count cut (50 / 100 / 200 turns)
2. The long-session published report's losses section honestly reports any quality regression at high turn counts (not suppressed)
3. `atelier bench run --suite terminalbench --quick --yes` completes in <5 min (single task, N=2) and prints a terminal comparison table showing cost, latency, and pass-rate per mode with delta
4. Running without `--yes` prints the cost estimate and exits without spending any tokens
5. Attempting a run with estimated cost >$50 without `--no-cost-cap` exits with an actionable error message
6. `atelier bench run --help` documents `--quick`, `--full`, `--suite`, `--seed`, `--yes`, `--no-cost-cap`, and the `publish` subcommand

**Dependencies**: Phase 3, Phase 5
**Plans**: TBD
**UI hint**: yes

---

### Phase 7: PR-Replay Benchmarks
**Goal**: Any developer can run `atelier bench run --pr <github-url>` to benchmark Atelier's impact on their own real GitHub PR — getting cost, latency, and diff-quality scores for both arms, with a non-Claude judge scoring the quality to avoid self-judging bias.
**Depends on**: Phase 6 (CLI), Phase 3 (A/B runner)
**Requirements**: PR-01, PR-02, PR-03, PR-04, PR-05, PR-06

**Tasks**:
- `PR-01/02`: Implement `atelier bench run --pr <github-url>` in `src/atelier/cli/commands/bench.py`
  - Fetch PR metadata via GitHub API: title, body, base commit SHA, real diff
  - Check out base commit in isolated git worktrees (one per arm) using `GitPython` or `pygit2` (already in stack)
  - Launch `claude -p` subprocess with PR title + body as prompt in each worktree under the respective bench mode
- `PR-03`: Score generated diff against real merged diff using `difflib.SequenceMatcher.ratio()` + hunk coverage (file overlap ratio)
- `PR-04`: LLM-as-judge rubric for weighted quality score; **judge model must be non-Claude** (GPT-4o or Gemini) to avoid self-judging bias; pin judge model name + version; include in report methodology section
- `PR-05`: Print per-PR comparison table: cost, latency, diff similarity score, judge score per arm with Δ
- `PR-06`: Store transcript JSON per replay under `~/.atelier/bench/<run-id>/`; each file includes all required fields (transcript, tokens, cost, latency, diff_similarity, judge_score)
- Validate against at least one real public PR (integration smoke test)

**Acceptance criteria**:
1. `atelier bench run --pr https://github.com/<owner>/<repo>/pull/<N>` fetches the PR, checks out the base commit in isolated worktrees, runs the agent under both bench modes, and produces a comparison table
2. Each arm runs in a separate git worktree — no shared working-tree state between on-arm and off-arm
3. Diff quality score uses `difflib.SequenceMatcher.ratio()` + hunk file-overlap; score is deterministic for identical diffs
4. LLM judge is confirmed non-Claude (GPT-4o or Gemini); judge model name and version appear in the report methodology section
5. Transcript JSON files are written to `~/.atelier/bench/<run-id>/` and contain all required fields
6. Integration smoke test against a real public PR completes without error and prints a comparison table with cost, latency, diff similarity, and judge score per arm

**Dependencies**: Phase 3, Phase 6
**Plans**: TBD

---

## Progress Table

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Bench-Mode Toggle | 0/? | Not started | - |
| 2. TerminalBench Adapter | 3/3 | Complete   | 2026-05-28 |
| 3. A/B Runner | 0/? | Not started | - |
| 4. Report Generator | 0/? | Not started | - |
| 5. Publication Pipeline | 0/? | Not started | - |
| 6. Long-Session Suite + User-Facing CLI | 0/? | Not started | - |
| 7. PR-Replay Benchmarks | 0/? | Not started | - |

---

## Coverage

| Requirement | Phase | Status |
|-------------|-------|--------|
| MODE-01 | Phase 1 | Complete |
| MODE-02 | Phase 1 | Complete |
| MODE-03 | Phase 1 | Complete |
| MODE-04 | Phase 1 | Complete |
| MODE-05 | Phase 1 | Complete |
| MODE-06 | Phase 1 | Complete |
| MODE-07 | Phase 1 | Complete |
| MODE-08 | Phase 1 | Complete |
| TB-01 | Phase 2 | Complete | 
| TB-02 | Phase 2 | Complete | 
| TB-03 | Phase 2 | Complete | 
| TB-04 | Phase 2 | Complete | 
| TB-05 | Phase 2 | Complete | 
| AB-01 | Phase 3 | Complete | 
| AB-02 | Phase 3 | Complete | 
| AB-03 | Phase 3 | Complete | 
| AB-04 | Phase 3 | Complete | 
| AB-05 | Phase 3 | Complete | 
| AB-06 | Phase 3 | Complete | 
| RPT-01 | Phase 4 | Complete | 
| RPT-02 | Phase 4 | Complete | 
| RPT-03 | Phase 4 | Complete | 
| RPT-04 | Phase 4 | Complete | 
| RPT-05 | Phase 4 | Complete | 
| RPT-06 | Phase 4 | Complete | 
| PUB-01 | Phase 5 | Complete | 
| PUB-02 | Phase 5 | Complete | 
| PUB-03 | Phase 5 | Complete | 
| PUB-04 | Phase 5 | Complete | 
| PUB-05 | Phase 5 | Complete | 
| LS-01 | Phase 6 | Complete | 
| LS-02 | Phase 6 | Complete | 
| LS-03 | Phase 6 | Complete | 
| LS-04 | Phase 6 | Complete | 
| CLI-01 | Phase 6 | Complete | 
| CLI-02 | Phase 6 | Complete | 
| CLI-03 | Phase 6 | Complete | 
| CLI-04 | Phase 6 | Complete | 
| CLI-05 | Phase 6 | Complete | 
| CLI-06 | Phase 6 | Complete | 
| PR-01 | Phase 7 | Complete | 
| PR-02 | Phase 7 | Complete | 
| PR-03 | Phase 7 | Complete | 
| PR-04 | Phase 7 | Complete | 
| PR-05 | Phase 7 | Complete | 
| PR-06 | Phase 7 | Complete | 

**v1 coverage: 47/47 requirements mapped ✓**

---

## Key Constraints (baked into acceptance criteria)

| Constraint | Where enforced |
|------------|---------------|
| Phase 1 is a hard blocker for all other phases | `Depends on` chain starts at Phase 1 |
| Phase 2 needs isolated Python 3.12 uv workspace | TB-01 acceptance criterion 5 |
| Phase 3 must interleave arm executions rep-by-rep | AB-02 acceptance criterion 5 |
| Separate `ATELIER_ROOT` per arm to prevent filesystem leakage | MODE-06 / Phase 1 acceptance criterion 5 |
| Never use tiktoken for published token counts — API `usage` field only | Phase 3 task note |
| Wilson score CI (not normal approximation) for pass-rate | AB-05 acceptance criterion 4 |
| Phase 5 must fix `blog: false` in docusaurus.config.ts | PUB-04 acceptance criterion 4 |
| Phase 7 must use non-Claude judge (GPT-4o or Gemini) | PR-04 acceptance criteria 4 |
| Module-level singletons require subprocess isolation between arms | Phase 1 / Phase 2 design constraint |
| `ATELIER_DEV_MODE=1` must not override bench-mode gating | Phase 1 task note |

---

*Roadmap created: 2026-05-28*
*Milestone target: v0.1 public benchmarks MVP*
*First report target: 2026-06-04 (D1–D5 critical path)*

---

## Milestone v0.2: Context Quality Lift

**Goal:** Make the same underlying model measurably better at coding tasks by feeding it better-scoped, history-aware context — and by refusing to break the KV-cache for marginal routing wins.

**Phases:**

- [x] **Phase 8: Context Lineage** — LLM-summarised commit history embedded alongside code chunks; agent can answer "why was this changed?" without reading raw git log
- [x] **Phase 9: Cache-Aware Routing** — Superseded by v0.3 Phase 12
- [ ] **Phase 10: Counterexample Loop** — Superseded by v0.3 Phase 14 (completed 2026-05-28)
- [ ] **Phase 11: Scoped Pull Context** — Superseded by v0.3 Phase 15

---

### Phase 8: Context Lineage
**Goal**: Every past commit in the repo is a retrievable, ranked context chunk — the agent can answer "why was this code changed?", "is there prior art for this pattern?", and "when did this regression appear?" without asking the host LLM to parse raw `git log` output.
**Depends on**: Phase 7 (v0.1 complete; builds on existing `infra/code_intel/git_history/walker.py` and `code_context` SQLite store)
**Requirements**: LINEAGE-01, LINEAGE-02, LINEAGE-03, LINEAGE-04, LINEAGE-05, LINEAGE-06, CQEVAL-01, CQEVAL-02

**Key modules**:
- `src/atelier/infra/code_intel/git_history/summarizer.py` (new) — commit → SemanticSummary via small LLM (Haiku 3.5 default)
- `src/atelier/infra/code_intel/git_history/embedder.py` (new) — summary → vector, persist via intel_store
- `src/atelier/core/capabilities/code_context/intel_store.py` (extend) — new `commit_chunks` SQLite table
- `src/atelier/core/capabilities/code_context/engine.py` (extend) — `search_symbols()` merges commit_chunks with `provenance="commit"`
- `tests/benchmarks/context_quality/` (new) — M1_lineage.py benchmark + README

**Success Criteria** (what must be TRUE):
  1. Full bootstrap walk completes on the Atelier repo without error and all 500 commits persist to the `commit_chunks` SQLite table; bootstrap is resumable if interrupted mid-walk
  2. Incremental update fires automatically on next session start whenever new commits exist; merge commits and >50-file commits are skipped by default
  3. `code op="search"` returns commit chunks merged into the ranked result list alongside symbol/file results; every commit result carries `provenance="commit"` and `commit_sha` fields
  4. `code op="search" provenance="commit"` filter returns only commit chunk results (no symbol/file results bleed through)
  5. M1 benchmark passes: ≥7/10 commit history queries answered with a correct citation; `tests/benchmarks/context_quality/` suite exists with README describing the eval protocol

**Plans**: 08-01a-PLAN.md, 08-01b-PLAN.md, 08-02-PLAN.md

---

### Phase 9: Cache-Aware Routing
**Goal**: The model router refuses to switch models when doing so would evict a KV-cache prefix whose reconstruction cost exceeds the estimated quality gain — and routes are sticky across a tool-call chain within a single agent turn, preventing cache thrashing.
**Depends on**: Nothing (independent; wires existing `prefix_cache/planner.py` into existing `model_routing/router.py`)
**Requirements**: CACHE-01, CACHE-02, CACHE-03, CACHE-04, CACHE-05, CQEVAL-03

**Key modules**:
- `src/atelier/core/capabilities/model_routing/cache_cost.py` (new) — pure function `cache_eviction_cost_usd(plan_a, plan_b, pricing)`
- `src/atelier/core/capabilities/model_routing/stickiness.py` (new) — turn-window state; resets on new user-visible response
- `src/atelier/core/capabilities/model_routing/router.py` (extend) — accept `prior_plan`, `current_plan`, `prior_route`, `stickiness_remaining` args; existing callers unchanged (all default to None)

**Success Criteria** (what must be TRUE):
  1. All existing callers of `ModelRouter.recommend()` compile and pass their tests without modification (new args are optional with None defaults)
  2. When `cache_eviction_cost_usd > estimated_quality_gain_usd`, the router returns the prior model; when quality gain exceeds cache cost, it switches normally
  3. Three consecutive tool-call `recommend()` invocations within one agent turn return the same model (stickiness window default = 3); stickiness counter resets when the agent emits a new user-visible response
  4. Every `recommend()` call emits a `route_decision` event to the run ledger containing `cache_cost_usd`, `quality_gain_usd_estimated`, `decision`, and `stickiness_remaining` fields
  5. M2 benchmark passes: ≥10% cost reduction on 50 replayed session traces with zero quality-tier regressions

**Plans**: TBD

---

### Phase 10: Counterexample Loop
**Goal**: When the agent produces code that fails a deterministic check (lint, typecheck, or scoped tests), the failure is fed back as a structured counterexample in the tool-result channel — giving the agent a second (and third) attempt to self-correct before the user sees any failure.
**Depends on**: Nothing (independent; Phase 9 makes retries cheaper but is not a hard dependency)
**Requirements**: COUNTER-01, COUNTER-02, COUNTER-03, COUNTER-04, COUNTER-05, CQEVAL-04

**Key modules**:
- `src/atelier/core/capabilities/verification/` (new) — `capability.py`, `counterexample.py`, `budget.py`, `checks/lint.py`, `checks/typecheck.py`, `checks/tests.py`, `checks/semantic_review.py`
- `src/atelier/core/capabilities/proof_gate/capability.py` (extend) — accept verification trace as evidence

**Success Criteria** (what must be TRUE):
  1. `VerifierCapability` runs lint, typecheck, and test checks scoped exclusively to files touched by the agent in the current attempt (no full-suite trigger)
  2. Each check failure produces a `Counterexample` dataclass with all required fields: `check`, `severity`, `file_path`, `line`, `diagnostic`, `expected`, `actual`, `repro_command`
  3. Counterexample blocks are injected into the agent via the tool-result channel only; the prompt compiler rejects any `Counterexample` block carrying Stability ≥ BRANCH (enforced by assertion, not convention)
  4. Retry loop caps at 3 attempts per subtask; on budget exhaustion `rescue.invoke(reason="verification_budget_exhausted")` is called — never silent failure
  5. M3 benchmark passes: ≥60% self-correction rate on 20 seeded type-error edits (baseline ≤15% without counterexamples)

**Plans**: TBD

---

### Phase 11: Scoped Pull Context
**Goal**: The agent (and host CLIs) can call `context op="pull"` with a subtask description to receive a minimal, budget-packed, rationale-annotated context bundle scoped to that subtask — including relevant commit summaries from M1 — replacing over-broad session-start retrieval with a pull-model that pushes toward context tightness.
**Depends on**: Phase 8 (SCOPED-06 requires M1 commit chunks in `search_symbols()` candidate set)
**Requirements**: SCOPED-01, SCOPED-02, SCOPED-03, SCOPED-04, SCOPED-05, SCOPED-06, CQEVAL-05

**Key modules**:
- `src/atelier/core/capabilities/scoped_context/` (new) — `capability.py`, `models.py` (Subtask, ScopedContext, ContextBudget), `pull.py`, `prune.py`
- MCP `context` tool (extend) — register `context op="pull"` accepting `subtask`, `budget_tokens`, `affected_paths`, `excluded_paths`

**Success Criteria** (what must be TRUE):
  1. `ScopedContextCapability.pull(subtask)` returns a `ScopedContext` with chunks ranked and packed within `subtask.budget_tokens` (default 4000); output token count never exceeds the budget
  2. No chunk whose path matches `subtask.excluded_paths` appears in any `ScopedContext` output, regardless of its ranking score
  3. `ScopedContext` includes `rationale` (citing top candidate scores), `excluded` (every dropped candidate with reason), and `trace_id`; a second call with an identical `Subtask` returns a cached result with `provenance="cached"`
  4. `context op="pull"` MCP op is registered and accessible to host CLIs; M1 commit chunks surface in results when the subtask description matches prior commit summaries
  5. M4 benchmark passes: precision ≥0.6 and recall ≥0.85 on 20 multi-file edits drawn from this repo's history

**Plans**: TBD
**UI hint**: no

---

## Milestone v0.3: Context Quality Execution

**Goal:** Finish the context-quality execution stack so local benchmarks prove the agent is cheaper, faster, and materially better at coding tasks without changing the underlying model.

**Phases:**

- [x] **Phase 12: Cache-Aware Routing** — Wire prefix-cache economics into model routing; keep routes sticky across tool-call chains and emit route-decision telemetry
- [x] **Phase 13: Phase-Linear Cache-Reuse Agent** — Add a Survey→Plan cache-warm run mode with minified read context, mode selection, and linear-vs-per-agent benchmark proof
- [ ] **Phase 14: Counterexample Loop** — Add scoped deterministic verification and structured counterexamples that drive bounded self-correction before failures reach the user
- [ ] **Phase 15: Scoped Pull Context + Proof Gate** — Add `context op="pull"`, scoped-context benchmarks, and TerminalBench-oriented local proof that Atelier-on is cheaper, faster, and targets ≥90% pass rate

---

### Phase 12: Cache-Aware Routing
**Goal**: The model router refuses to switch models when doing so would evict a KV-cache prefix whose reconstruction cost exceeds the estimated quality gain, and routes stay sticky across follow-up tool calls.
**Depends on**: Phase 8 (route decisions can now consider context lineage search cost but do not require it)
**Requirements**: CACHE-01, CACHE-02, CACHE-03, CACHE-04, CACHE-05, CQEVAL-03

**Key modules**:
- `src/atelier/core/capabilities/model_routing/cache_cost.py` (new) — pure cache eviction cost calculation from prefix plans and pricing
- `src/atelier/core/capabilities/model_routing/stickiness.py` (new) — turn-window sticky route state
- `src/atelier/core/capabilities/model_routing/router.py` (extend) — optional cache-plan and prior-route inputs; existing callers unchanged
- `tests/benchmarks/context_quality/M2_routing.py` (extend) — replay-cost benchmark

**Success Criteria**:
  1. Existing `ModelRouter.recommend()` callers compile without changes because new inputs are optional.
  2. Synthetic cache plans prove the router stays on the prior route when cache eviction cost exceeds estimated quality gain, and switches when the gain justifies the cost.
  3. A default three-call stickiness window keeps follow-up tool calls on the same route and resets on a user-visible response boundary.
  4. Every recommendation emits a `route_decision` ledger event with cache cost, estimated quality gain, decision, and stickiness fields.
  5. M2 benchmark proves ≥10% estimated cost reduction with no quality-tier regressions.

**Plans**: `.planning/phases/12-cache-aware-routing/12-01-PLAN.md`
**Summary**: `.planning/phases/12-cache-aware-routing/12-01-SUMMARY.md`

---

### Phase 13: Phase-Linear Cache-Reuse Agent
**Goal**: Make multi-phase coding runs cheaper and faster at the same model quality by running Survey and Plan as one cache-warm conversation, minifying read context, and selecting linear mode only when it wins.
**Depends on**: Phase 12 (uses cache telemetry and pricing; can be developed in parallel where pure)
**Requirements**: LINEAR-01, LINEAR-02, LINEAR-03, LINEAR-04, LINEAR-05, TBEVAL-01

**Key modules**:
- `src/atelier/core/capabilities/context_reuse/models.py` (extend) — phase state machine models and cache stats
- `src/atelier/core/capabilities/context_reuse/phase_runner.py` (new) — Survey→Plan→Implement orchestration
- `src/atelier/core/capabilities/context_reuse/prompts/` (new) — fixed system prompt and per-phase user objectives
- `src/atelier/core/capabilities/context_compression/` (extend) — safe `minify_source()` read-context path
- `src/atelier/core/runtime/engine.py` (extend) — `linear | per_agent | auto` dispatch

**Success Criteria**:
  1. Unit tests prove Survey and Plan share one message list and one fixed system prompt; Implement starts lean as a writer step.
  2. Cache breakpoint and cache-read/write/fresh-input/output token stats are recorded per phase in the run ledger.
  3. Minified reads preserve Python/YAML semantics, reduce read-context tokens measurably, and are never used for writer exact-byte reads.
  4. `auto` chooses linear for context-sharing scenarios and falls back for divergent or oversized contexts.
  5. Linear-vs-per-agent benchmark artifact shows ≥30% lower cost and ≥25% lower wall-time at equal-or-better task success.

**Plans:** 4 plans
- [x] 13-01-PLAN.md — Phase state-machine models, fixed prompts, PhaseRunner, additive ledger fields (LINEAR-01, LINEAR-02)
- [x] 13-02-PLAN.md — minify_source + reader/writer profile dispatch + minify telemetry (LINEAR-03)
- [x] 13-03-PLAN.md — Engine run_phased dispatch with auto fallback heuristic (LINEAR-04)
- [x] 13-04-PLAN.md — Local linear-vs-per_agent benchmark + threshold-proving artifact (LINEAR-05, TBEVAL-01)

**Summary**: `.planning/phases/13-phase-linear-cache-reuse-agent/13-04-SUMMARY.md`
**Verification**: `.planning/phases/13-phase-linear-cache-reuse-agent/13-VERIFICATION.md`

Locked design reference: docs/plans/phase-linear-cache-reuse/01-PLAN.md

---

### Phase 14: Counterexample Loop
**Goal**: Deterministic check failures become structured counterexamples in the tool-result channel, allowing bounded self-correction inside the agent loop.
**Depends on**: Phase 12 (cheaper retries) and Phase 13 (linear run mode should preserve cache stability by keeping counterexamples out of static prompts)
**Requirements**: COUNTER-01, COUNTER-02, COUNTER-03, COUNTER-04, COUNTER-05, CQEVAL-04

**Key modules**:
- `src/atelier/core/capabilities/verification/` (new) — verifier, checks, counterexample model, retry budget
- `src/atelier/core/capabilities/proof_gate/capability.py` (extend) — verification trace as evidence
- `src/atelier/core/capabilities/prompt_compilation/` (extend) — reject Counterexample blocks with static/branch stability
- `tests/benchmarks/context_quality/M3_verification.py` (extend) — self-correction benchmark

**Success Criteria**:
  1. Verifier runs lint, typecheck, tests, and semantic checks scoped to touched files only.
  2. Failures render as structured `Counterexample` objects with check, severity, location, diagnostic, expected/actual, and repro command.
  3. Prompt compiler enforces counterexamples in tool-result/turn stability, never system/static stability.
  4. Retry budget caps at three attempts and invokes rescue on exhaustion.
  5. M3 benchmark proves ≥60% self-correction on seeded type-error edits.

**Plans**: TBD

---

### Phase 15: Scoped Pull Context + Proof Gate
**Goal**: `context op="pull"` returns minimal subtask-scoped context with rationale/exclusion trace, and final local benchmarks prove the v0.3 stack is faster, cheaper, and high-quality.
**Depends on**: Phase 8, Phase 12, Phase 13, Phase 14
**Requirements**: SCOPED-01, SCOPED-02, SCOPED-03, SCOPED-04, SCOPED-05, SCOPED-06, CQEVAL-05, TBEVAL-02

**Key modules**:
- `src/atelier/core/capabilities/scoped_context/` (new) — Subtask, ScopedContext, pull/prune/cache logic
- `src/atelier/core/capabilities/code_context/engine.py` (reuse) — code and commit candidate search
- `src/atelier/gateway/adapters/mcp_server.py` (extend) — register `context op="pull"`
- `tests/benchmarks/context_quality/M4_scoped.py` (extend) — precision/recall benchmark
- TerminalBench/local proof harness — record pass rate, cost, and latency deltas

**Success Criteria**:
  1. `ScopedContextCapability.pull()` returns ranked chunks within budget and excludes forbidden paths deterministically.
  2. Output includes rationale, excluded records, trace ID, and cached provenance on repeated identical pulls.
  3. `context op="pull"` is available to host CLIs and can surface M1 commit chunks when relevant.
  4. M4 benchmark reaches precision ≥0.6 and recall ≥0.85 on multi-file edits from this repo history.
  5. Final local proof run targets ≥90% TerminalBench pass rate while cheaper and faster than baseline; failures loop back into implementation before sign-off.

**Plans**: TBD

---

## Progress Table (v0.2)

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 8. Context Lineage | 3/3 | Complete | 2025-07-15 |
| 9. Cache-Aware Routing | 0/? | Superseded by Phase 12 | - |
| 10. Counterexample Loop | 0/? | Superseded by Phase 14 | - |
| 11. Scoped Pull Context | 0/? | Superseded by Phase 15 | - |

## Progress Table (v0.3)

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 12. Cache-Aware Routing | 1/1 | Complete    | 2026-05-28 |
| 13. Phase-Linear Cache-Reuse Agent | 4/4 | Complete | 2026-05-29 |
| 14. Counterexample Loop | 0/? | Not started | - |
| 15. Scoped Pull Context + Proof Gate | 0/? | Not started | - |

---

## Coverage (v0.2)

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

**v0.2 coverage: 8/8 requirements mapped ✓**

## Coverage (v0.3)

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

**v0.3 coverage: 26/26 requirements mapped ✓**

---

*v0.2 roadmap appended: 2026-05-28*
*Milestone target: v0.2 Context Quality Lift*
*Build order: Phase 8 → Phase 9 → Phase 10 → Phase 11 (Phase 9 can run parallel with Phase 8)*
*v0.3 roadmap appended: 2026-05-28*
*Milestone target: v0.3 Context Quality Execution*
*Build order: Phase 12 → Phase 13 → Phase 14 → Phase 15*
