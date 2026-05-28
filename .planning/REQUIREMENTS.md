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

**Coverage:**
- v1 requirements: 47 total
- Mapped to phases: 47
- Unmapped: 0 ✓

---
*Requirements defined: 2026-05-28*
*Last updated: 2026-05-28 after initial definition*
