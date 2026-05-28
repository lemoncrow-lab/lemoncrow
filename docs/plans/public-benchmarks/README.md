# Public benchmarks — reproducible Atelier-on vs Atelier-off A/B

Status: **active** · Initiated 2026-05-28 · Owner: coding agent
Related: [../../../strategy.md](../../../strategy.md), [../../../roadmap.md](../../../roadmap.md), [../savings-honest-ab/README.md](../savings-honest-ab/README.md)

## Why

Within 48 hours of opening Atelier publicly (2026-05-26), multiple developers asked for the same thing, unprompted:

- "What this project needs are benchmarks. Showing off time and tokens saved over a week of prompting."
- "Have you done treatment/control (using canonical harnesses) experiments on some benchmark like TerminalBench to quantify savings / quality tradeoffs?"
- "As long as I don't see benchmarks proving both efficiency + same quality as without atelier, I won't try it."

The ask is **end-to-end agent A/B**: run the same task with Atelier-on and Atelier-off, on a canonical harness (TerminalBench, SWE-bench), report cost, latency, *and* quality, publish raw transcripts.

What we have today does **not** answer this:

- `benchmarks/mcp_tools/` measures tool-level token deltas (read/grep/edit vs cat/rg) on synthetic cases. Not end-to-end agent quality.
- `benchmarks/swe/atelier_proxy.py` + `make_preds.py` runs SWE-bench predictions but lacks an explicit Atelier-off control arm and the publication shape devs want.
- `src/atelier/infra/benchmarks/publisher.py` produces internal weekly snapshots, not externally-reproducible blog-shaped reports.

## Non-negotiable rules (apply to every deliverable)

1. **Three metrics, always together:** cost ($), latency (s), quality (pass/fail or grader score). Never publish cost-only.
2. **Raw transcripts published.** Every cell in every table links to a per-run transcript file.
3. **Publish losses.** When Atelier-on loses a cell (slower, costlier, lower quality), publish it.
4. **N ≥ 5 runs per cell** with seeded determinism. Report mean + 95% CI.
5. **Reproducible by a stranger.** Every published report must include the exact CLI command and commit SHA to reproduce.

## Deliverables

Ordered by dependency. D1–D5 are the critical path to **benchmark report #1 by 2026-06-04**. D6–D7 follow.

---

### D1 — `bench-mode` toggle: Atelier-on vs Atelier-off

**Why:** A/B requires a clean "off" arm. Atelier-off must behave as if Atelier weren't installed: no routing rewrites, no compaction, no memory read/write, no tool substitution.

**Build:**
- New module: `src/atelier/bench/mode.py`
- Env var: `ATELIER_BENCH_MODE` ∈ {`on`, `off`, `unset`}. Unset = production behavior.
- When `off`:
  - `ModelRouter` returns the caller's requested model unchanged (no downtiering, no rerouting).
  - `ContextCompactor` is a passthrough (no compaction, no LLM hint extraction).
  - Memory adapters (Claude/Codex/Gemini readers) are disabled; `atelier memory list` returns empty.
  - MCP tool substitution disabled: native equivalents are used (the agent's own `Read`/`Grep`, not `mcp__atelier__read`/`grep`).
  - All routing/compact telemetry tagged `bench_mode=off` so it's separable in analysis.
- A single bootstrap call early in process startup reads `ATELIER_BENCH_MODE` and installs the off-mode shims.

**Touch:**
- `src/atelier/core/routing/model_router.py` — add `if bench_mode_off(): return passthrough`.
- `src/atelier/core/context/compactor.py` (or equivalent) — same passthrough.
- `src/atelier/gateway/adapters/mcp_server.py` — register/skip MCP tools based on mode.
- `src/atelier/cli/main.py` (or equivalent entrypoint) — read env var once, log mode at start.

**Acceptance:**
- `ATELIER_BENCH_MODE=off atelier --version` runs without invoking router or compactor (verify via debug log).
- Unit test: `test_bench_mode_off_disables_router`, `test_bench_mode_off_disables_compactor`, `test_bench_mode_off_disables_mcp_tools`.
- Integration test: same agent prompt under `mode=on` and `mode=off` produces measurably different token counts in telemetry.

---

### D2 — TerminalBench adapter

**Why:** TerminalBench is the canonical harness developers named. We need to run its tasks with our agent under both bench modes.

**Build:**
- New package: `benchmarks/terminalbench/`
  - `runner.py` — loads TerminalBench task definitions, runs the agent against each task, captures the grader's pass/fail.
  - `agent_adapter.py` — thin wrapper that invokes `claude -p` (or `codex`/`gemini`) as a subprocess with the task prompt, captures stdout/stderr/tool-calls, returns a transcript object.
  - `tasks.yaml` — the pinned subset of TerminalBench task IDs we run (start with 10).
- TerminalBench should be a pinned git submodule or PyPI dep, not vendored copy.
- Capture per-run: full transcript, token counts (input/output/cache), wall-clock latency, grader verdict, $.

**Touch:**
- `pyproject.toml` — add TerminalBench dependency (or document submodule).
- `benchmarks/terminalbench/` — new files above.

**Acceptance:**
- `python -m benchmarks.terminalbench.runner --task <task-id> --mode off --model claude-sonnet` produces a transcript JSON with all fields populated.
- `--mode on` works identically and produces a distinguishable transcript.
- Runs against the pinned 10-task subset complete in <30 min on a single machine.

---

### D3 — A/B runner with N replications and seeded determinism

**Why:** Single runs are noise. Need N ≥ 5 per cell with seeded prompts/temperatures, mean + 95% CI.

**Build:**
- New module: `benchmarks/ab/runner.py` building on existing `benchmarks/mcp_tools/harness.py` patterns (`BenchCase`, `CaseResult`) but for end-to-end agent runs, not per-tool.
- CLI: `python -m benchmarks.ab.runner --suite terminalbench --tasks 10 --n 5 --models claude-sonnet --modes on,off --out bench/runs/<run-id>/`
- Output layout: `bench/runs/<run-id>/{config.json, raw/<task-id>__<mode>__<rep>.json, summary.json}`.
- `summary.json` aggregates: mean cost, mean latency, pass-rate per (task, mode), with 95% CI.
- Resumable: if killed mid-run, re-running with same `--run-id` skips completed cells.
- Deterministic seed: `--seed 42` propagates to agent temperature and task ordering.

**Touch:**
- `benchmarks/ab/runner.py`, `benchmarks/ab/__init__.py`, `benchmarks/ab/aggregate.py`.
- May lift shared utilities (token counting via `tiktoken`) from `benchmarks/mcp_tools/harness.py`.

**Acceptance:**
- Full sweep (10 tasks × 2 modes × 5 reps = 100 runs) completes and produces a `summary.json` with all cells populated.
- Killing mid-sweep and rerunning produces the same final summary (verify by comparing checksums).
- Same `--seed` produces same task ordering across two runs.

---

### D4 — Metrics aggregator + plot generator

**Why:** Three required plots: cost delta, latency delta, quality delta (pass-rate). Each with 95% CI bars.

**Build:**
- New module: `benchmarks/ab/report.py`.
- Input: a `summary.json` produced by D3.
- Output: `bench/runs/<run-id>/plots/{cost_delta.png, latency_delta.png, quality_delta.png}` and `report.md`.
- `report.md` structure:
  - Methodology section (model, N, harness, commit SHA, CLI command)
  - Headline table: per-task `Atelier-on | Atelier-off | Δ | 95% CI` for each of cost, latency, pass-rate.
  - Embedded plot images.
  - Per-task transcript links to `raw/<task>__<mode>__<rep>.json`.
  - **Losses section** that explicitly enumerates cells where Atelier-on did worse.
- Plotting: `matplotlib` (already in deps) or `plotly` if interactive HTML is preferred.

**Touch:**
- `benchmarks/ab/report.py`.
- `benchmarks/ab/templates/report.md.j2` — jinja2 template for the markdown report.

**Acceptance:**
- `python -m benchmarks.ab.report <run-id>` produces 3 PNG files and `report.md`.
- The losses section appears even when Atelier-on wins all cells (with a row stating "no losses this run").
- `report.md` renders cleanly on GitHub (verify by pushing a draft).

---

### D5 — Publication pipeline for external posts

**Why:** Internal `src/atelier/infra/benchmarks/publisher.py` produces weekly snapshots for our own dashboards. External posts need a different shape: standalone, blog-ready, transcript-bundled.

**Build:**
- New module: `src/atelier/infra/benchmarks/external_publisher.py` (mirrors `publisher.py` style, but external-shaped).
- CLI: `atelier bench publish <run-id> --out docs-site/blog/<slug>/`
- Output:
  - `docs-site/blog/<slug>/index.md` — the post (extends `report.md` with intro/conclusion sections).
  - `docs-site/blog/<slug>/transcripts/` — copies of raw transcripts.
  - `docs-site/blog/<slug>/plots/` — copies of plots.
  - `docs-site/blog/<slug>/reproduce.sh` — the exact CLI command that reproduces the run.
- Front-matter for the docusaurus blog: title, date, authors, tags `[benchmark, atelier-vs-baseline, <model>]`.

**Touch:**
- `src/atelier/infra/benchmarks/external_publisher.py`.
- `src/atelier/cli/commands/bench.py` — add `publish` subcommand.
- `docs-site/blog/` — new posts will land here.

**Acceptance:**
- `atelier bench publish <run-id> --out docs-site/blog/2026-06-04-terminalbench-claude-sonnet/` creates a self-contained directory with `index.md`, transcripts/, plots/, reproduce.sh.
- The `reproduce.sh` script, run on a fresh clone with API keys, regenerates the same `summary.json` (modulo non-deterministic agent variance).
- The post renders correctly in the existing docusaurus site (`docs-site/`).

---

### D6 — Long-session quality-degradation suite

**Why:** The most-cited objection: "If Atelier compacts more aggressively than Codex/Claude, does it lose context? Are stored reasoning blocks stale?" This suite directly answers it.

**Build:**
- New suite: `benchmarks/ab/suites/long_session.py`.
- Defines tasks that require maintaining context across 50, 100, and 200 turns (e.g., multi-file refactor, long debugging session with state references back to turn-1 setup).
- Runs same task at each cut: 50-turn version, 100-turn version, 200-turn version.
- Quality metric: graded by a held-out evaluator LLM with a rubric scoring fact recall and consistency vs the turn-1 setup.
- Reuses D1–D4 infrastructure; only adds the task definitions and the recall-rubric grader.

**Touch:**
- `benchmarks/ab/suites/long_session.py`.
- `benchmarks/ab/graders/recall_rubric.py`.
- `benchmarks/ab/suites/long_session_tasks.yaml`.

**Acceptance:**
- `python -m benchmarks.ab.runner --suite long_session --n 5` produces a summary showing quality-delta by turn-count cut.
- The published report's losses section is honest about any quality regression at high turn counts.

---

### D7 — User-facing `atelier bench` CLI

**Why:** Devs asked "who has the time and tokens to run this themselves?" Answer: give them a one-command quick mode that runs in <5 min on their own keys and prints a local comparison.

**Build:**
- Extend `src/atelier/cli/commands/bench.py` (D5 added `publish`; D7 adds `run`).
- CLI: `atelier bench run --suite terminalbench --quick`
  - `--quick`: 1 task, N=2, both modes. Runs in <5 min.
  - `--full`: 10 tasks, N=5, both modes. Same as the published config.
- Prints a terminal-formatted comparison table on completion: cost, latency, pass-rate per mode with delta.
- Stores results under `~/.atelier/bench/<run-id>/` for later `atelier bench publish`.
- Honest disclaimer printed at start: estimated cost in $ before the user proceeds. Requires `--yes` to skip.

**Touch:**
- `src/atelier/cli/commands/bench.py`.
- README.md root — add "Reproduce our benchmarks" section pointing to `atelier bench run --quick`.

**Acceptance:**
- `atelier bench run --help` documents both subcommands.
- `atelier bench run --suite terminalbench --quick --yes` completes in <5 min on a single task and prints a comparison table.
- Repeated runs with different seeds produce different but statistically consistent results.

---

## Sequencing for benchmark report #1 (ship by 2026-06-04)

1. **Day 1–2:** D1 (bench-mode toggle) — unblocks everything else.
2. **Day 2–3:** D2 (TerminalBench adapter).
3. **Day 3–4:** D3 (A/B runner with N reps).
4. **Day 4–5:** D4 (plots + report.md).
5. **Day 5–6:** D5 (publication pipeline).
6. **Day 6:** Run the full sweep, generate the first published post, push to `docs-site/`.
7. **Day 7:** Promote: reply to every developer who asked, share on HN/Bluesky.

D6 and D7 are week-2/3 work, not blocking the first report.

## Dependencies

```
D1 (mode toggle) ─┬─> D2 (TerminalBench adapter) ─┐
                  │                                 ├─> D3 (A/B runner) ─> D4 (plots) ─> D5 (publish)
                  └─────────────────────────────────┘                                       │
                                                                                            ├─> D6 (long-session)
                                                                                            └─> D7 (atelier bench CLI)
```

## Validation — the whole plan succeeds if

- A first benchmark report is published at `docs-site/blog/2026-06-04-*` covering TerminalBench × Claude Sonnet × 10 tasks × N=5.
- A stranger can clone the repo, run `reproduce.sh`, and regenerate the same `summary.json`.
- The post includes both a wins section and a losses section.
- The 20+ developers who asked for benchmarks each receive a personal link reply.

## Open questions

- Which agent process do we use under TerminalBench: `claude -p` subprocess (matches existing `benchmarks/swe/atelier_proxy.py` pattern), or our own agent loop? Subprocess is simpler and more credible ("we used Anthropic's own CLI"). Default: subprocess.
- Where do plot CIs come from for pass-rate (binary metric)? Wilson score interval, not normal approximation. Encode in D4.
- Should `atelier bench run` require a separate API key env var or reuse `ANTHROPIC_API_KEY`? Default: reuse, but print which is being used.
- Cost cap for `--full`: should we hard-stop if the estimated cost exceeds $50? Default: yes, with `--no-cost-cap` to override.
