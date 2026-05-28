# Feature Landscape: Reproducible AI Agent Benchmarking System

**Domain:** Public A/B benchmarking for AI developer tools (Atelier-on vs Atelier-off)
**Researched:** 2026-05-28
**Research mode:** Ecosystem / requirements definition

---

## What Makes a Benchmark Credible to Skeptical Developers

This is the root question. The developers who asked Atelier for benchmarks within 48 hours
of launch are not naive — they've seen cherry-picked "we saved 40% of tokens" posts that
don't show quality tradeoffs. The credibility bar is set by SWE-bench, Aider's polyglot
benchmark, and Terminal-Bench (tbench.ai). The common thread:

**Credibility comes from what you publish, not what you achieved.**

### The non-negotiable credibility primitives (all must be present):

| Signal | Why it matters | How to implement |
|--------|----------------|------------------|
| Raw transcripts per run | "Show me the work" — devs want to see if the agent cheated or hallucinated | `transcripts/<task>__<mode>__<rep>.json` linked from every table row |
| Explicit losses section | Cherry-picking wins is the biggest red flag. Publishing losses signals honesty more than any win | `## Losses` section even when all cells are wins (say "no losses this run") |
| N ≥ 5 per cell with CI | "N=1 proves nothing" is the second most common objection | Wilson score interval for binary pass/fail; normal approximation is wrong at low N |
| Commit SHA + `reproduce.sh` | A stranger can run it and get the same result | `reproduce.sh` at repo root of each published post; exact CLI command in methodology section |
| Three metrics together | Cost-only metrics are dishonest. Quality-only metrics hide waste. | Cost ($), latency (s), quality (pass-rate or grader score) always appear as a triplet |
| Docker/sandboxed isolation | Without isolation, environment differences explain everything | Terminal-Bench and SWE-bench both use containerized eval for this reason |

**Confidence:** HIGH — these requirements are directly stated by the 20+ developers who asked, corroborated by Aider's published methodology, SWE-bench's Docker isolation mandate, and Terminal-Bench's per-task breakdown format.

---

## Table Stakes

Features that must exist for the benchmark to be taken seriously at all. Missing any one
of these = developers dismiss the report as marketing, not measurement.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| **A/B toggle (on/off)** | Without a clean control arm, the comparison is meaningless | Medium | `ATELIER_BENCH_MODE` env var; must disable routing, compaction, memory, and tool substitution in `off` mode |
| **Per-run transcript files** | Every credible benchmark (SWE-bench, Aider, TerminalBench) links raw session data | Low | JSONL with full tool calls, token counts, grader verdict per run |
| **Three metrics in every table** | Cost-only was the explicit complaint from developers; quality alone ignores economics | Low | cost ($), latency (s), pass-rate — always together in the headline table |
| **Losses section in every report** | Absence of losses signals cherry-picking; presence signals integrity | Low | Even if empty: "No losses in this run. Atelier-on won all 10 tasks on all three metrics." |
| **Reproducibility artifact** | "A stranger can clone and reproduce" — stated requirement from the project plan | Medium | `reproduce.sh` + pinned commit SHA in every published post |
| **95% CI on pass-rate** | N=1 claims are routinely called out; CI quantifies meaningful differences | Medium | Wilson score interval (not normal approximation) for binary pass/fail |
| **Named harness (Terminal-Bench)** | Developers trust benchmarks that use external, independent harnesses over internal synthetic tasks | Medium | TerminalBench @ tbench.ai as submodule or PyPI dep (not vendored) |
| **Seeded determinism** | Reproducibility requires same task ordering across runs | Low | `--seed 42` propagates to agent temperature and task ordering |

---

## Differentiators

Features that set this benchmark apart. No other AI tool benchmarking system does all of these.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| **PR-replay benchmark** | "Run a benchmark on YOUR codebase's own history" — personal relevance destroys objections | High | See detailed spec below |
| **`atelier bench run --quick`** | Removes the "I don't have time/tokens to reproduce it" objection | Medium | 1 task, N=2, <5 min, prints comparison table, prepaid cost disclaimer |
| **Long-session degradation suite** | Directly addresses "does Atelier lose context after aggressive compaction?" | High | 50/100/200-turn tasks with recall-rubric grader |
| **Resumable runs** | Full benchmarks take hours; interruption should not waste work | Medium | Kill mid-run, re-run with same `--run-id` skips completed cells |
| **Cost cap with override** | Prevents accidental $500 bills; shows respect for developer economics | Low | Hard-stop at $50 default; `--no-cost-cap` to override |
| **Live terminal progress** | Benchmark runs feel fast when you can watch them; silence reads as "stuck" | Low | Rich live table with per-task status, elapsed time, current cost |
| **Published to Docusaurus blog** | External benchmark reports should look like writing, not raw data dumps | Medium | `atelier bench publish` assembles blog post directory with intro, plots, transcript links |
| **Interactive delta plots** | Three PNG plots (cost Δ, latency Δ, quality Δ) with CI bars, one per report | Low | matplotlib; per-task bars, sorted by quality delta descending |

---

## Feature Deep Dives

### PR-Replay Benchmark (`--pr <url>`)

**What it is:** `atelier bench run --pr https://github.com/org/repo/pull/123` checks out the
base commit (before the PR merged), runs the agent twice (Atelier-on, Atelier-off) with the
PR title + body as the prompt, scores the generated diffs against the real merge commit, and
reports cost + latency + quality delta.

**Why it's powerful:** It creates a complete benchmark cell from real development history:
- Base commit = reproducible starting state
- PR title + body = the actual task prompt a developer would give an agent
- Real merge = ground truth for what "correct" looks like

**Diff quality scoring — what to measure and how:**

| Score | What it measures | Implementation | Confidence |
|-------|-----------------|----------------|------------|
| **Line overlap ratio** | Textual similarity of agent diff vs real diff | `difflib.SequenceMatcher.ratio()` on unified diff text | HIGH — in Python stdlib, industry standard for diff similarity |
| **Hunk coverage** | What fraction of the real PR's changed files did the agent touch? | `files_touched_agent / files_touched_real` | HIGH — direct from patch parsing |
| **Test pass-through** | Does applying the agent's patch make the same tests pass as the real PR? | Run `git apply <agent_patch>` then `pytest` in sandbox | HIGH — most reliable quality signal, but requires test suite |
| **LLM-as-judge rubric** | Holistic code quality: correctness, style, completeness, no regressions | Claude/GPT-4 judge with per-criterion rubric (see below) | MEDIUM — judge scoring has inter-rater variance; use N=3 judge calls |

**LLM-as-judge rubric for diff quality** (recommended criteria with weights):

```
Criteria for PR-replay judge:
- [weight: 25] The generated patch resolves the stated issue in the PR title/body
- [weight: 20] No files are modified that were not modified in the real PR
- [weight: 20] The changed lines are functionally equivalent to the real PR changes
- [weight: 15] The patch applies cleanly (no conflict markers)
- [weight: 10] Code style matches the repo's existing style
- [weight: -30] The patch introduces new test failures or syntax errors
```

Use `PerCriterionGrader` pattern (from the Rubric library / MT-bench / lm-sys approach):
each criterion evaluated independently, scores aggregated with weights, final score 0–1.
Run judge 3× and take median to reduce variance.

**Composite diff quality score:**
```
diff_quality = 0.3 * line_overlap + 0.2 * hunk_coverage + 0.3 * llm_judge + 0.2 * test_pass_rate
```
When test suite is unavailable, redistribute weights: line_overlap=0.4, hunk_coverage=0.25, llm_judge=0.35.

**What the report shows:**
```
PR-Replay: github.com/org/repo/pull/123 — "Add rate limiting to API endpoints"
Base commit: a3f7c9d

                    Atelier-on    Atelier-off    Δ
Cost                $0.023        $0.041         -44% ✓
Latency             47s           89s            -47% ✓
Diff quality        0.81          0.67           +21% ✓
  ↳ line overlap    0.79          0.61
  ↳ hunk coverage   0.90          0.80
  ↳ LLM judge       0.78          0.64
  ↳ test pass rate  pass          pass

[Transcript: on-rep1] [Transcript: off-rep1]
```

---

### Long-Session Quality Degradation Suite

**Why it matters:** The objection is: "Atelier's compaction is more aggressive than Claude's
built-in. Does it lose context? Are stored reasoning blocks stale by turn 100?"

**Task design:** Tasks that require referencing information established at turn 1 to correctly
complete work at turn 50, 100, and 200. Examples:
- Multi-file refactor: specs established at turn 1, implementation at turn 50+
- Debugging session: error described at turn 1, root cause confirmed at turn 100+
- Architecture decision: constraints set at turn 1, API surface finalized at turn 200

**Quality metric — recall rubric grader:**

The grader at each turn-count cut asks:

```
Given:
  - The task setup from turns 1-5: <first_5_turns>
  - The agent's output at turn N: <output_at_turn_N>

Score these criteria:
  [weight: 40] The agent correctly references the constraint/fact established at turn 1
  [weight: 30] The agent's solution is consistent with the original requirements
  [weight: 20] The agent has not re-asked for information already provided
  [weight: -30] The agent contradicts a decision made in the first 10 turns
```

**What the report shows:**

```
Long-session quality degradation: claude-sonnet-4 × N=5

Turn count    Atelier-on quality    Atelier-off quality    Δ
50 turns      0.94 [0.89–0.99]      0.92 [0.86–0.98]       +2%
100 turns     0.91 [0.85–0.97]      0.88 [0.81–0.95]       +3%
200 turns     0.87 [0.79–0.95]      0.79 [0.70–0.88]       +10% ✓
              cost: -38% ✓          latency: -22% ✓
```

The compelling claim: Atelier-on degrades *less* at high turn counts because stored
reasoning blocks preserve context more durably than raw token context.

---

### `atelier bench run --quick` UX Spec

**What skeptical developers actually want:** Run it in 5 minutes, on their own keys, see
the same result as the published post. The entire value is "I verified this myself."

**Pre-run (before any API calls):**
```
$ atelier bench run --suite terminalbench --quick

  Atelier Benchmark: quick mode
  ─────────────────────────────
  Suite:    TerminalBench (1 task, N=2 per mode)
  Model:    claude-sonnet-4 (from ANTHROPIC_API_KEY=sk-ant-...4abc)
  Harness:  TerminalBench @ commit e3f8a1c (pinned)

  Estimated cost: ~$0.40–$0.80 total (4 agent runs × ~$0.10–$0.20 each)
  Note: actual cost may be higher if tasks require long sessions.
  A hard cost cap of $50 is enforced. Use --no-cost-cap to remove.

  Proceed? [y/N]: 
```

**During run (Rich live table):**
```
  Running benchmark...

  Task                    Mode         Status      Cost     Time
  ─────────────────────── ──────────── ─────────── ──────── ──────
  crack-7z-hash           Atelier-on   ✓ pass      $0.11    42s
  crack-7z-hash           Atelier-off  ✓ pass      $0.19    78s
  crack-7z-hash           Atelier-on   ✓ pass      $0.09    38s
  crack-7z-hash           Atelier-off  ⟳ running…  $0.14…   61s…
```

**After run (comparison table):**
```
  ┌─────────────────────────────────────────────────────────────────┐
  │  Atelier Benchmark Results — crack-7z-hash × N=2               │
  ├────────────────┬──────────────┬──────────────┬──────────────────┤
  │                │  Atelier-on  │  Atelier-off │  Delta           │
  ├────────────────┼──────────────┼──────────────┼──────────────────┤
  │  Cost          │  $0.10       │  $0.19       │  -47% ✓          │
  │  Latency       │  40s         │  77s         │  -48% ✓          │
  │  Pass rate     │  2/2 (100%)  │  2/2 (100%)  │  0% (tied)       │
  ├────────────────┴──────────────┴──────────────┴──────────────────┤
  │  Note: N=2 is too small for reliable CI. Use --full for N=5.   │
  │  Run ID: 2026-05-28-crack-7z-hash-42                           │
  │  Transcripts: ~/.atelier/bench/2026-05-28-crack-7z-hash-42/    │
  └─────────────────────────────────────────────────────────────────┘

  To publish: atelier bench publish 2026-05-28-crack-7z-hash-42
  To reproduce: atelier bench run --suite terminalbench --quick --seed 42 --yes
```

**Design principles for `--quick`:**
1. Print the API key being used (truncated) before spending anything
2. Cost estimate with range before asking for consent
3. Live progress — every run updates the table in real time
4. Final table uses color: green for wins (Δ positive for Atelier), red for losses
5. Small-N warning built into the footer (not footnote — front and center)
6. Run ID is always printed so the developer can share it or publish it
7. Transcripts stored locally at deterministic path for audit

---

### Report Format (what developers want to see)

Based on Aider's polyglot benchmark, SWE-bench reports, and Terminal-Bench leaderboard format:

**Mandatory sections in every published report:**

1. **TL;DR** — one sentence per metric: "Atelier-on cut cost 44% and latency 48% with no
   quality regression on 10 TerminalBench tasks (N=5 each, Claude Sonnet 4, 2026-06-04)."

2. **Methodology** — exact model, N, harness version, commit SHA, seed, CLI command to reproduce

3. **Headline table** — per-task × per-metric, one row per task, three metric columns each
   showing `on | off | Δ [95% CI]`

4. **Three delta plots** — cost Δ, latency Δ, quality Δ — sorted by task, with CI bars,
   PNG format (embeds in Docusaurus)

5. **Losses section** — every cell where Atelier-on lost (any metric), with the magnitude.
   If empty: explicit statement "No losses in this run."

6. **Per-task transcript links** — table of `task × mode × rep → transcript file`. Not a
   footnote; a named section with direct links.

7. **Reproduction instructions** — `git clone`, `pip install`, `reproduce.sh`, estimated cost

**What NOT to include:**
- Absolute scores without comparison (meaningless without baseline)
- Cherry-picked "representative" tasks (publish all tasks in the pinned set)
- Aggregate-only tables (must show per-task breakdown — devs check if you're hiding failures)
- Claims about models not covered in the run (only claim what you measured)

---

## Anti-Features

Features to explicitly NOT build (stated or implied by the project's non-negotiable rules):

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| **Cost-only benchmark** | The first non-negotiable rule. Misleading without quality signal. | Always report cost + latency + quality as a triplet |
| **Internal-only snapshots** | What `publisher.py` already does. Not reproducible, not externally credible. | External publication pipeline (`bench publish`) with self-contained blog post directory |
| **Vendored TerminalBench copy** | Destroys reproducibility guarantee; fork drift invalidates comparisons | Pinned git submodule or PyPI dep with exact version |
| **N=1 published claims** | Any developer familiar with benchmarking will immediately dismiss | Enforce N≥2 in `--quick`, N≥5 in `--full`, warn loudly if user tries less |
| **Benchmark without losses** | Cherry-picking is the biggest credibility destroyer in the AI benchmarking space | Losses section is mandatory, always present, never hidden |
| **A/B without isolation** | If Atelier-off mode still routes or compacts behind the scenes, the comparison is invalid | `ATELIER_BENCH_MODE=off` must be a clean passthrough — no routing, no compaction, no MCP tools |
| **LLM-judge without stated model/version** | Judge results are not reproducible if you don't pin the judge model | Always report judge model + version + temperature in methodology section |
| **Benchmarks on synthetic tasks only** | Internal synthetic tasks invite "you optimized for your own tests" objection | TerminalBench external harness + real GitHub PR history (PR-replay) |

---

## Feature Dependencies

```
BENCH-01 (mode toggle)
  └─→ BENCH-02 (TerminalBench adapter)
        └─→ BENCH-03 (A/B runner, N reps, CI)
              ├─→ BENCH-04 (plots + report.md with losses)
              │     └─→ BENCH-05 (publication pipeline, reproduce.sh)
              ├─→ BENCH-06 (long-session suite: 50/100/200 turns + recall rubric)
              └─→ BENCH-07 (atelier bench run --quick / --full CLI)

BENCH-08 (PR-replay) depends on:
  - BENCH-01 (mode toggle)
  - BENCH-03 (A/B runner infrastructure)
  - BENCH-04 (report generation with diff quality section)
  - GitHub API + git checkout capability
  - LLM-as-judge for diff quality scoring (new dependency, not in other suites)
```

---

## MVP Recommendation

Prioritize D1–D5 for the 2026-06-04 target. These produce the first publishable report
that answers developer objections:

**Must ship (week 1):**
1. BENCH-01: Mode toggle — unblocks everything
2. BENCH-02: TerminalBench adapter — external harness = credibility
3. BENCH-03: A/B runner with N=5, Wilson CI — "N≥5 with CI" objection answered
4. BENCH-04: Report with losses section — "publish losses" objection answered
5. BENCH-05: Publication pipeline — blog post with `reproduce.sh`

**Defer (week 2–3):**
- BENCH-06 (long-session): Powerful but not needed for report #1
- BENCH-07 (CLI): Nice UX but devs can run `python -m benchmarks.ab.runner` for now
- BENCH-08 (PR-replay): Highest differentiation but highest complexity

**The minimum for a report that will not be dismissed:**
> TerminalBench × 10 tasks × N=5 × Claude Sonnet 4 × Atelier-on vs Atelier-off
> with raw transcripts, losses section, 95% CI, and a `reproduce.sh`.

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Credibility requirements (transcripts, losses, CI, reproduce.sh) | HIGH | Directly stated by developers; corroborated by SWE-bench, Aider, and TerminalBench methodology |
| TerminalBench as the harness | HIGH | Named explicitly by developers in their requests; tbench.ai has public leaderboard and HuggingFace dataset |
| Diff quality scoring approach | MEDIUM | `difflib.SequenceMatcher` and LLM-as-judge are well-established; composite weights are project judgment calls requiring validation |
| Long-session recall rubric grader | MEDIUM | The concept is sound (re-read rate, error drift are already in `compact_quality_bench.py`); specific rubric criteria need iteration against real sessions |
| `atelier bench run --quick` UX | HIGH | Rich tables + cost disclaimer + live progress are standard patterns in Python CLIs (Aider does this); specific layout is design judgment |
| Wilson score CI for pass-rate | HIGH | Standard statistics; normal approximation is wrong for binary metrics at low N (well-documented) |
| LLM-as-judge stability | LOW | Judge agreement rates vary by model; results differ between judge models and versions; this needs inter-rater reliability testing before publishing |

---

## Sources

- Terminal-Bench leaderboard: https://www.tbench.ai / https://github.com/fugue-labs/tbview
- Terminal-Bench task examples: https://datasets-server.huggingface.co/first-rows?dataset=harborframework/terminal-bench-2-leaderboard
- Aider polyglot benchmark methodology: https://github.com/Aider-AI/aider/blob/main/benchmark/README.md
- SWE-bench evaluation harness: https://github.com/swe-bench/swe-bench (Context7 verified)
- SWE-agent: https://github.com/swe-agent/swe-agent (Context7 verified)
- MT-bench LLM-as-judge: https://github.com/lm-sys/FastChat/tree/main/fastchat/llm_judge
- Rubric library (LLM-as-judge per-criterion): Context7 ID `/the-llm-data-company/rubric`
- Rich terminal tables: Context7 ID `/textualize/rich`
- Wilson score interval: standard statistics; referenced in project plan (BENCH-03 / D3 section)
- Existing Atelier compact_quality_bench.py: `/home/pankaj/Projects/leanchain/atelier/src/benchmarks/swe/compact_quality_bench.py` — establishes patterns for error_drift and re-read metrics
- Existing Atelier savings_replay.py: reuse tiktoken, cost estimation patterns
