# Roadmap: Atelier Public Benchmarks

**Milestone:** v0.1 — Public Benchmarks MVP
**Core Value:** A stranger can clone the repo, run one command, and reproduce the exact benchmark results we published — including the losses.
**Created:** 2026-05-28
**Critical path:** Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 (first published report by 2026-06-04)

---

## Phases

- [ ] **Phase 1: Bench-Mode Toggle** — Clean `ATELIER_BENCH_MODE` on/off toggle that gates router, compactor, memory, and MCP tools; unblocks every downstream phase
- [ ] **Phase 2: TerminalBench Adapter** — Isolated Python 3.12 workspace running TerminalBench tasks via `claude -p` subprocess, capturing full transcript + metrics per run
- [ ] **Phase 3: A/B Runner** — Interleaved N-rep execution with seeded determinism, resumability, and Wilson-score `summary.json`
- [ ] **Phase 4: Report Generator** — Three delta plots (cost/latency/quality) and a `report.md` with methodology, headline table, transcript links, and explicit losses section
- [ ] **Phase 5: Publication Pipeline** — `atelier bench publish` assembles a self-contained Docusaurus blog post; fixes `blog: false` in docusaurus.config.ts
- [ ] **Phase 6: Long-Session Suite + User-Facing CLI** — Recall-rubric grader for 50/100/200-turn degradation, plus `atelier bench run --quick/--full` with Rich progress and cost gate
- [ ] **Phase 7: PR-Replay Benchmarks** — `atelier bench run --pr <url>` replays any GitHub PR A/B with diff-quality scoring; non-Claude judge to avoid self-judging bias

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
| 2. TerminalBench Adapter | 0/? | Not started | - |
| 3. A/B Runner | 0/? | Not started | - |
| 4. Report Generator | 0/? | Not started | - |
| 5. Publication Pipeline | 0/? | Not started | - |
| 6. Long-Session Suite + User-Facing CLI | 0/? | Not started | - |
| 7. PR-Replay Benchmarks | 0/? | Not started | - |

---

## Coverage

| Requirement | Phase | Status |
|-------------|-------|--------|
| MODE-01 | Phase 1 | Pending |
| MODE-02 | Phase 1 | Pending |
| MODE-03 | Phase 1 | Pending |
| MODE-04 | Phase 1 | Pending |
| MODE-05 | Phase 1 | Pending |
| MODE-06 | Phase 1 | Pending |
| MODE-07 | Phase 1 | Pending |
| MODE-08 | Phase 1 | Pending |
| TB-01 | Phase 2 | Pending |
| TB-02 | Phase 2 | Pending |
| TB-03 | Phase 2 | Pending |
| TB-04 | Phase 2 | Pending |
| TB-05 | Phase 2 | Pending |
| AB-01 | Phase 3 | Pending |
| AB-02 | Phase 3 | Pending |
| AB-03 | Phase 3 | Pending |
| AB-04 | Phase 3 | Pending |
| AB-05 | Phase 3 | Pending |
| AB-06 | Phase 3 | Pending |
| RPT-01 | Phase 4 | Pending |
| RPT-02 | Phase 4 | Pending |
| RPT-03 | Phase 4 | Pending |
| RPT-04 | Phase 4 | Pending |
| RPT-05 | Phase 4 | Pending |
| RPT-06 | Phase 4 | Pending |
| PUB-01 | Phase 5 | Pending |
| PUB-02 | Phase 5 | Pending |
| PUB-03 | Phase 5 | Pending |
| PUB-04 | Phase 5 | Pending |
| PUB-05 | Phase 5 | Pending |
| LS-01 | Phase 6 | Pending |
| LS-02 | Phase 6 | Pending |
| LS-03 | Phase 6 | Pending |
| LS-04 | Phase 6 | Pending |
| CLI-01 | Phase 6 | Pending |
| CLI-02 | Phase 6 | Pending |
| CLI-03 | Phase 6 | Pending |
| CLI-04 | Phase 6 | Pending |
| CLI-05 | Phase 6 | Pending |
| CLI-06 | Phase 6 | Pending |
| PR-01 | Phase 7 | Pending |
| PR-02 | Phase 7 | Pending |
| PR-03 | Phase 7 | Pending |
| PR-04 | Phase 7 | Pending |
| PR-05 | Phase 7 | Pending |
| PR-06 | Phase 7 | Pending |

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
