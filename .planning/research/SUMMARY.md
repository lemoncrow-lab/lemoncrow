# Project Research Summary

**Project:** Atelier Public Benchmarks  
**Domain:** Reproducible A/B AI agent benchmarking (Atelier-on vs Atelier-off)  
**Researched:** 2026-05-28  
**Confidence:** HIGH — all claims verified against installed packages, live codebase inspection, and live `claude -p` invocation

---

## Executive Summary

Atelier needs a reproducible, externally credible A/B benchmark that compares its routing/compaction/memory capabilities ("Atelier-on") against a clean vanilla Claude baseline ("Atelier-off"). The benchmark audience — developers who asked for this within 48 hours of launch — are experienced and skeptical; they've seen cherry-picked AI benchmarks before. Credibility is entirely determined by what you *publish*, not what you *achieved*: raw transcripts, a mandatory losses section, N≥5 per cell with Wilson CI, a commit-pinned `reproduce.sh`, and cost+latency+quality as an inseparable triplet. The harness of record is **TerminalBench** (tbench.ai) — the developers named it directly, and it provides Docker-isolated task execution essential for reproducible results.

The recommended approach is a new `benchmarks/ab/` module built on top of TerminalBench's PyPI package (`terminal-bench==0.2.18`), driven by a dedicated Python 3.12 uv workspace (separate from the main 3.11 project). Each benchmark arm runs as a subprocess via `claude -p --output-format stream-json --verbose` with a per-arm isolated `ATELIER_ROOT` directory, ensuring no state bleeds between the on/off arms. The `ATELIER_BENCH_MODE=on|off` env var is read once at process startup via a new `bench/mode.py` singleton that installs passthrough shims across routing, compression, memory, and MCP tool registration — one clean guard per capability, never scattered inline.

The key risks are subtle contamination bugs that invalidate the A/B comparison: shared `ATELIER_ROOT` state, module-level singletons surviving in-process arm switches, existing memory on disk in `~/.atelier/`, auto-update mutating code mid-sweep, and prompt caching asymmetry when arms run in batch order instead of interleaved. These are well-understood and have concrete prevention patterns — all documented in `PITFALLS.md`. The statistical risk (Wald CI at N=5 gives nonsensical `[1.0, 1.0]` bounds for perfect pass-rates) is solved with a 10-line pure-math Wilson interval implementation that avoids any scipy dependency.

---

## Key Findings

### Recommended Stack

The core stack reuses Atelier's existing dependencies aggressively: Click, Rich, Jinja2, Pydantic, and subprocess-based `claude -p` invocation are all already present. The only genuinely new dependency is `terminal-bench==0.2.18` (PyPI), which requires Python ≥ 3.12, resolved by a separate `benchmarks/pyproject.toml` uv workspace while keeping the main project on 3.11. `matplotlib>=3.9` must be added for delta plots; scipy should NOT be added (Wilson CI is implemented as pure math to avoid import failures seen in the current venv).

**Core technologies:**
- **`terminal-bench==0.2.18` (PyPI):** External benchmark harness — Docker-isolated task execution, `Harness`/`BaseAgent`/`BenchmarkResults` API, pass@k scoring. PyPI pin, not submodule (explicit PROJECT.md requirement).
- **`claude -p --output-format stream-json --verbose`:** Agent subprocess — authoritative cost/token/latency data in the `result` line; exactly how a developer would run it (credibility signal).
- **`ATELIER_BENCH_MODE` env var + `bench/mode.py` singleton:** Clean A/B toggle — read once at startup via `bootstrap()`, shims installed at capability entry points only.
- **File-per-cell checkpoint (`raw/<task>__<mode>__rep<N>.json`):** Resumable runs — presence of file = run completed; `os.replace()` for atomic writes.
- **Wilson CI (pure math, `benchmarks/ab/stats.py`):** Correct CI for binary pass-rate — fixes Wald's `[1.0, 1.0]` failure at N=5 perfect pass.
- **`matplotlib>=3.9` + Agg backend:** Headless delta plots (cost Δ, latency Δ, quality Δ) as PNG — no kaleido, no plotly.
- **Jinja2 (already transitive):** Report + blog post templating — make explicit dep in benchmarks extras.
- **`diff-match-patch` (already in deps):** PR-replay diff quality scoring — Levenshtein ratio of unified diffs.
- **Python 3.12 uv workspace (`benchmarks/pyproject.toml`):** Isolates terminal-bench version conflict from main project's Python 3.11 requirement.

**What NOT to use:** pandas, plotly, scipy.proportion_confint (broken in current venv), Wald/normal CI (mathematically wrong at low N), Anthropic SDK for agent runs (use `claude -p`), tiktoken for published token counts (use API `usage` field — 10-30% error otherwise), the existing `publisher.py` (internal format, do not extend).

### Expected Features

**Must have (table stakes) — all required for the benchmark to be taken seriously:**
- **A/B toggle (ATELIER_BENCH_MODE on/off):** Clean baseline — routing, compaction, memory, and MCP tools all disabled in `off` mode.
- **Per-run transcript files:** `transcripts/<task>__<mode>__rep<N>.json` — every credible benchmark links raw session data (SWE-bench, Aider, TerminalBench all do this).
- **Three-metric table:** Cost ($), latency (s), pass-rate — always together; cost-only was the explicit developer complaint.
- **Losses section in every report:** Mandatory and front-and-center (second section, not last); absence of losses = cherry-picking signal.
- **Reproducibility artifact:** `reproduce.sh` + pinned commit SHA in every published post; tested on a fresh clone.
- **Wilson 95% CI on pass-rate:** Correct formula for binary metric at N=5; Wald is mathematically wrong.
- **TerminalBench as external harness:** Named by developers; external independence = credibility.
- **Seeded determinism:** `--seed 42` propagates to task ordering.

**Should have (differentiators) — set this benchmark apart:**
- **`atelier bench run --quick`:** 1 task, N=2, <5 min, live Rich table — removes "I don't have time to reproduce" objection.
- **Long-session quality degradation suite:** 50/100/200-turn tasks with recall-rubric grader — directly addresses "does aggressive compaction lose context?" question.
- **Resumable runs (`--run-id`):** Kill mid-run, re-run skips completed cells — critical for 100-cell sweeps.
- **Cost cap ($50 default) with `--no-cost-cap` override:** Shows developer-economics respect.
- **Live terminal progress (Rich live table):** Per-task status, elapsed time, current cost.
- **Three delta plots (matplotlib PNG):** cost Δ, latency Δ, quality Δ with CI bars, sorted by quality delta.
- **Docusaurus blog publication pipeline:** `atelier bench publish <run-id>` assembles self-contained blog post directory.

**Defer to v2+:**
- **PR-replay benchmark (`--pr <url>`):** Highest differentiation but highest complexity (git worktrees, diff scoring, LLM-as-judge, GitHub API). Requires D1–D3 infrastructure first; LLM judge stability is LOW confidence.
- **`atelier bench run --full` interactive UX polish:** Developers can run `python -m benchmarks.ab.runner` for now.

### Architecture Approach

The architecture has a clean dependency spine: a new `bench/mode.py` singleton unblocks everything by providing the toggle primitive; a `benchmarks/ab/` module with schema → runner → aggregate → report is the straight-line build path; a new `external_publisher.py` (explicitly not extending the existing `publisher.py`) assembles the final blog post. Every arm runs as a subprocess with an isolated temp `ATELIER_ROOT`, making cross-contamination structurally impossible. The `benchmarks/ab/` module has no import dependency on the existing `benchmarks/mcp_tools/harness.py` — they share only coding patterns, not code.

**Major components:**
1. **`src/atelier/bench/mode.py`** — bootstrap singleton + `is_off()` predicate; shims installed in routing, compression, memory, and MCP tool registration (one guard per capability).
2. **`benchmarks/ab/schema.py`** — `AgentRunCase` / `AgentRunResult` dataclasses; data contract for the entire pipeline.
3. **`benchmarks/terminalbench/agent_adapter.py`** — subprocess wrapper for `claude -p`; constructs isolated env dict per arm; parses stream-json `result` line.
4. **`benchmarks/ab/runner.py` (`ABRunner`)** — file-per-cell checkpoint loop; `os.replace()` atomic writes; interleaved arm execution (on/rep1, off/rep1, on/rep2, ...).
5. **`benchmarks/ab/aggregate.py`** — collects `raw/*.json` → `summary.json` with Wilson CI; stores raw `{passed, total}` counts (not p_hat).
6. **`benchmarks/ab/report.py`** — `summary.json` → three matplotlib delta plots + `report.md` via Jinja2.
7. **`src/atelier/infra/benchmarks/external_publisher.py`** — assembles `docs-site/blog/<slug>/` directory: plots, transcript Markdown summaries, `reproduce.sh` from config.json.
8. **`src/atelier/gateway/cli/bench_commands.py`** — Click group `atelier bench run / publish`; registered in `app.py` via `cli.add_command(bench_group)`.

**Build order (dependency graph):**
```
D1: bench/mode.py + shims (standalone — unblocks everything)
    │
    ├─→ D2: agent_adapter.py + benchmarks/ab/schema.py
    │         │
    │         └─→ D3: runner.py + aggregate.py (Wilson CI)
    │                   │
    │                   └─→ D4: report.py (plots + report.md)
    │                             │
    │                             └─→ D5: external_publisher.py + docusaurus blog enabled
    │                                       │
    │                                       └─→ D7: bench_commands.py (CLI UX, --quick)
    │
    └─→ D6: long_session suite (parallel with D7, reuses D3 runner)
    └─→ D8: pr_replay suite (parallel, requires D1+D3+D4 + GitHub API)
```

### Critical Pitfalls

1. **Shared `ATELIER_ROOT` contaminates both arms** — `RealtimeContextManager`, `CostTracker`, `RunLedger`, `_append_savings()` all write to `ATELIER_ROOT` at runtime. *Prevention:* Each arm gets `bench_run_dir/atelier_root_{on,off}` as its `ATELIER_ROOT` via env var; delete or snapshot after each replication.

2. **Module-level singletons survive in-process arm switches** — seven singletons in `mcp_server.py` (ledger, realtime ctx, runtime cache, session IDs) are never reset between arm calls. *Prevention:* Always run each arm as a **subprocess** — the process boundary guarantees fresh module state. Never call tool handlers in-process for bench runs.

3. **Subtle off-mode leaks invalidate the baseline** — `@mcp_tool` decorators fire at import time (before `ATELIER_BENCH_MODE` is read), `ATELIER_DEV_MODE=1` overrides visibility filtering, `CLAUDE_CODE` env var triggers session hooks, auto-update can mutate code mid-sweep. *Prevention:* `bench/mode.py` must bootstrap before any module imports; set `ATELIER_NO_AUTO_UPDATE=1`; construct subprocess env dict explicitly (whitelist approach, not inherit).

4. **tiktoken `cl100k_base` is wrong for Claude token counting** — 10–30% error depending on content type; asymmetric error between arms exaggerates or understates savings. *Prevention:* Always use `usage` field from `claude -p stream-json` `result` line for published metrics; tiktoken only for pre-flight estimates.

5. **Post-hoc task selection (p-hacking)** — Running 30 tasks, publishing the 10 best results is technically true but statistically fraudulent; destroys credibility when independent researchers reproduce on the other 20. *Prevention:* **Pre-register** `benchmarks/terminalbench/tasks.yaml` with the 10 task IDs and selection criterion **before the first benchmark run**. Commit SHA must precede run SHA.

6. **Wald CI at N=5 is mathematically wrong** — `k=5, n=5` yields `[1.0, 1.0]` (impossible point CI); `k=0, n=5` yields `[0.0, 0.0]`. *Prevention:* Use the pure-math Wilson implementation in `benchmarks/ab/stats.py`; store raw `{passed: k, total: n}` in `summary.json` so CI is computed at report time.

7. **Prompt caching asymmetry** — Running all on-arm reps then all off-arm reps means the second batch inherits warm cache from the first (billed at ~10% rate), making the cost comparison arm-order-dependent. *Prevention:* Interleave executions: on/rep1, off/rep1, on/rep2, off/rep2, ... Report `cache_read_input_tokens` separately in summary table.

---

## Implications for Roadmap

Based on combined research, the feature dependency graph from FEATURES.md maps directly to a natural 5-phase build sequence, with two parallel tracks for stretch features.

### Phase 1: Bench Mode Toggle & Data Contracts
**Rationale:** `ATELIER_BENCH_MODE` is the single blocker for everything. Until it exists with correct passthrough shims across all 4 capability touch points, no valid A/B comparison can be made. Data schema (`AgentRunCase`/`AgentRunResult`) should be locked here too — downstream phases depend on it.  
**Delivers:** `bench/mode.py` singleton + shims in routing/compression/memory/MCP; `benchmarks/ab/schema.py`; verified smoke test (off-arm produces zero ledger events).  
**Addresses:** BENCH-01 (mode toggle).  
**Avoids:** Pitfalls 1, 2, 3 (contamination via shared state, singletons, import-time leaks).  
**Research flag:** Standard pattern — no additional research needed.

### Phase 2: TerminalBench Adapter & A/B Runner
**Rationale:** The external harness + runner is the core engine. Task pre-registration (`tasks.yaml`) must happen here — committing the pinned 10-task subset before any real run is the anti-p-hacking guarantee.  
**Delivers:** `benchmarks/terminalbench/agent_adapter.py` (subprocess wrapper, whitelist env), `benchmarks/ab/runner.py` (ABRunner + file-per-cell checkpoint + interleaved arms), pre-registered `tasks.yaml`, Python 3.12 uv workspace.  
**Addresses:** BENCH-02, BENCH-03 (harness adapter, A/B runner with N=5).  
**Avoids:** Pitfalls 4, 6, 7 (tiktoken, p-hacking, prompt cache asymmetry) by design of the runner.  
**Research flag:** Standard patterns — subprocess execution, checkpoint pattern are well-documented.

### Phase 3: Aggregation & Statistical Output
**Rationale:** Wilson CI must be baked in from the start, not retrofitted. `summary.json` schema (storing raw `{passed, total}` counts) is the contract between runner and all downstream consumers.  
**Delivers:** `benchmarks/ab/aggregate.py` (summary.json with Wilson CI), `benchmarks/ab/stats.py` (pure-math Wilson, no scipy), verified CI outputs at N=5 boundary cases (0/5, 5/5).  
**Addresses:** BENCH-03 statistical correctness.  
**Avoids:** Pitfall 8 (Wald CI failure).  
**Research flag:** Standard statistics — no research needed. Wilson formula verified in STACK.md.

### Phase 4: Report Generation & Plots
**Rationale:** The first publishable artifact. Losses section placement (second section, not last) and per-task breakdown are credibility requirements that belong in the template, not in developer judgment at publish time.  
**Delivers:** `benchmarks/ab/report.py`, three matplotlib delta plots (PNG, Agg backend), Jinja2 report template with mandatory losses section, `<!-- truncate -->` placement, headless-safe matplotlib config.  
**Addresses:** BENCH-04 (report with losses section, plots).  
**Avoids:** Pitfall 9 (empty/uncredible losses section), PITFALLS 15/17 (MDX breakage, missing truncate).  
**Research flag:** Standard patterns — matplotlib Agg is well-documented.

### Phase 5: Publication Pipeline & CLI
**Rationale:** The publication pipeline must be separate from the existing internal `publisher.py` (different audience, different output contract). Docusaurus `blog: false` must be flipped in the same PR as first publish, not as an afterthought. The CLI (`atelier bench run / publish`) wraps phases 1–4 with cost confirmation, live Rich table, and `--quick` mode.  
**Delivers:** `external_publisher.py`, `reproduce.sh` generation, Docusaurus blog enabled + navbar link, blog folder structure (not flat files), `bench_commands.py` CLI with `--quick`/`--full` presets, cost cap, `--yes` flag.  
**Addresses:** BENCH-05 (publication), BENCH-07 (`--quick` CLI UX).  
**Avoids:** Pitfalls 14–17 (Docusaurus: blog disabled, MDX parsing, image paths, truncate marker).  
**Research flag:** Standard patterns — Docusaurus blog folder structure is well-documented in PITFALLS.md.

### Phase 6 (Parallel Track A): Long-Session Degradation Suite
**Rationale:** Deferred from MVP but high audience value — directly answers "does Atelier's aggressive compaction lose context at turn 100+?" with data. Reuses the Phase 2–3 runner/aggregator unchanged; only adds a new suite module and a recall-rubric grader.  
**Delivers:** `benchmarks/ab/suites/long_session.py`, recall-rubric grader (50/100/200-turn tasks), grader token costs tracked separately (not attributed to bench run cost).  
**Addresses:** BENCH-06 (long-session suite).  
**Avoids:** Phase-specific warning from PITFALLS.md: grader LLM costs must not be summed into bench cost; use separate API cost bucket.  
**Research flag:** Grader rubric criteria need iteration against real long sessions — **needs research-phase during planning**. Concept is sound but specific criteria require tuning.

### Phase 7 (Parallel Track B): PR-Replay Benchmark
**Rationale:** Highest differentiation (benchmark on YOUR own codebase's real history), but highest complexity and lowest confidence in LLM-as-judge stability. Should only be built after the core pipeline (phases 1–5) is stable and at least one TerminalBench report is published.  
**Delivers:** `benchmarks/ab/suites/pr_replay.py`, git worktree isolation (per-arm fresh clone), diff scoring via `diff-match-patch`, LLM-as-judge rubric (median of 3 judge calls), `--pr <url>` CLI flag.  
**Addresses:** BENCH-08 (PR-replay).  
**Avoids:** Pitfall 10 (non-deterministic checkout, ground truth ambiguity — use `--pr-sha` override, pin base SHA in config.json).  
**Research flag:** **Needs research-phase during planning** — LLM-as-judge stability (LOW confidence), inter-rater reliability, judge model pinning, and composite weight validation all need investigation before committing to a scoring approach.

### Phase Ordering Rationale

- **Phases 1→2→3→4→5 are strictly sequential** (each phase's output is the next phase's input — mode toggle → runner → aggregator → report → publisher).
- **Phase 2 task pre-registration is a credibility gate** — no benchmark run can precede the `tasks.yaml` commit. The roadmapper should enforce this as a blocking deliverable in Phase 2.
- **Phases 6 and 7 are parallel tracks** that slot in after Phase 5 is shipped. Phase 6 is lower risk and should precede Phase 7.
- **The MVP for the first publishable report is Phases 1–5 only.** This produces: TerminalBench × 10 tasks × N=5 × Claude Sonnet 4 × Atelier-on vs Atelier-off, with raw transcripts, losses section, Wilson CI, and `reproduce.sh` — the minimum bar to not be dismissed.

### Research Flags

**Needs deeper research during planning:**
- **Phase 6 (long-session grader):** Recall-rubric criteria need tuning against real 50–200 turn sessions. The rubric shape is known but weights and pass threshold need empirical validation.
- **Phase 7 (PR-replay + LLM judge):** Judge stability, composite weight selection, inter-rater reliability, and ground truth diff ambiguity all need investigation. Do not commit to an implementation plan without researching existing LLM-as-judge literature (MT-bench, Rubric library) and testing on real PRs.

**Standard patterns — skip research-phase:**
- **Phase 1 (bench mode toggle):** Module-level singleton + enum pattern is standard Python. Shim registration pattern is documented in ARCHITECTURE.md with full code.
- **Phase 2 (TerminalBench adapter):** Subprocess execution via `claude -p` pattern already exists in `benchmarks/swe/atelier_proxy.py`. File-per-cell checkpoint is standard.
- **Phase 3 (Wilson CI):** Pure math, verified formula in STACK.md. No ambiguity.
- **Phase 4 (matplotlib plots):** Well-documented. Agg backend for headless CI is standard.
- **Phase 5 (Docusaurus publication):** Specific pitfalls fully documented (blog: false, MDX, folder structure, truncate marker). No unknowns.

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All technologies verified against live installed packages and live `claude -p` output. Wilson CI formula verified against boundary cases. tiktoken limitation confirmed by inspection. |
| Features | HIGH | Credibility requirements directly stated by the 20+ developers who requested benchmarks; corroborated by SWE-bench, Aider, and TerminalBench methodology. MVP feature set is clear. |
| Architecture | HIGH | Based on direct codebase inspection of `mcp_server.py`, `benchmarks/mcp_tools/harness.py`, `docusaurus.config.ts`. Component boundaries and file locations are specific and verified. |
| Pitfalls | HIGH | All critical pitfalls confirmed by direct code inspection with specific file and line references. The contamination vectors (shared ATELIER_ROOT, module singletons, import-time registration) are real and would silently corrupt results. |

**Overall confidence: HIGH**

### Gaps to Address

- **LLM-as-judge stability (Phase 7):** Judge scoring has inter-rater variance; LOW confidence that a specific rubric will produce consistent results without empirical testing. Treat PR-replay as a research task, not an implementation task, until judge stability is validated.
- **Long-session recall rubric criteria (Phase 6):** The rubric structure is sound (from `compact_quality_bench.py` patterns) but specific criteria weights need iteration against real 200-turn sessions. Don't finalize the rubric in the design phase — plan for iteration.
- **TerminalBench task selection:** The 10-task subset from TerminalBench must be selected and committed to `tasks.yaml` before Phase 2 implementation starts. The selection criterion (e.g., "first 10 alphabetically from medium-difficulty category") should be decided and documented in the roadmap, not deferred to implementation.
- **Anthropic non-determinism at temperature=0:** MEDIUM confidence that seeded runs will be reproducible — multi-GPU inference is not guaranteed deterministic. Methodology section of published reports should acknowledge this and report variance across reps rather than claiming exact reproducibility.

---

## Sources

### Primary (HIGH confidence)
- Direct codebase inspection: `src/atelier/gateway/adapters/mcp_server.py` (lines 85–167, 153–157, 528–549) — singleton contamination vectors
- Direct codebase inspection: `benchmarks/mcp_tools/harness.py` (lines 14–18) — tiktoken cl100k_base usage
- Direct codebase inspection: `src/atelier/infra/runtime/benchmarking.py` (lines 49–54) — simulated token counts
- Direct codebase inspection: `docs-site/docusaurus.config.ts` — `blog: false` confirmed
- Live `claude -p --output-format stream-json --verbose` invocation (2026-05-28) — `result` line schema verified
- Live `uv pip install terminal-bench==0.2.18` + wheel inspection — API classes and `requires-python = ">=3.12"` verified
- Anthropic SDK Python v0.104.1 source inspection via Context7 — `usage` object fields confirmed
- Docusaurus v3.x docs via Context7 — blog front matter, `<!-- truncate -->`, folder-based posts, MDX parsing behavior

### Secondary (MEDIUM confidence)
- Terminal-Bench leaderboard (tbench.ai) and HuggingFace dataset — task format and grader patterns
- Aider polyglot benchmark methodology (GitHub) — losses section and per-task breakdown requirements
- SWE-bench evaluation harness (GitHub via Context7) — Docker isolation mandate, reproducibility standard
- Wilson score interval: standard statistics (well-documented, referenced in PROJECT.md explicitly)
- MT-bench LLM-as-judge (lm-sys/FastChat) — per-criterion rubric pattern
- Rubric library (Context7 ID: `/the-llm-data-company/rubric`) — grader patterns for Phase 6/7

### Tertiary (LOW confidence)
- LLM-as-judge inter-rater reliability: widely reported issue but no formal benchmarks against Atelier's specific rubric criteria — **needs empirical validation in Phase 7**
- Anthropic API non-determinism at temperature=0: widely reported, not formally documented — methodology must acknowledge this

---

*Research completed: 2026-05-28*  
*Ready for roadmap: yes*
