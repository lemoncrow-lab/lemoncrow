# LemonCrow Benchmarks

This document keeps benchmark proof out of the first-use README while preserving the evidence trail for the headline claims.

> **Raw results and datasets** are in a separate repository: [lemoncrow-lab/benchmarks](https://github.com/lemoncrow-lab/benchmarks)

**Quick definitions:** *Input tok* = fresh tokens sent that turn. *Cache write* = context stored for reuse (billed once). *Cache read* = reused cached context (billed at a steep discount vs fresh input -- this is why cutting cache-read tokens saves less money than the token-count drop implies). *pp* = percentage points. *MRR / rec@1 / p95* (Code Search table) = mean reciprocal rank (higher is better) / recall at rank 1 / 95th-percentile latency.

## Headline Results


| Benchmark                                                                                                                                                                                                                                                               |                 LemonCrow result |                        Baseline |                       Delta |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------: | --------------------------------: | ----------------------------: |
| SWE-bench Verified, 50 sampled tasks x 5 reps                                                                                                                                                                                                                           | **232 / 250 resolved (92.8%)** |               202 / 250 (80.8%) | **+12.0 percentage points** |
| SWE-bench cost                                                                                                                                                                                                                                                          |          **$165.45** | $234.84 |               **29.5% cheaper** |                             |
| SWE-bench total tokens                                                                                                                                                                                                                                                  |                     **106.2M** |                          192.8M |             **44.9% fewer** |
| SWE-bench turns                                                                                                                                                                                                                                                         |                      **4,336** |                           6,962 |             **37.7% fewer** |
| SWE-bench wall-clock time                                                                                                                                                                                                                                               |                      **10.9h** |                           14.3h |            **23.7% faster** |
| SWE-bench Lite, 10 tasks x 5 reps                                                                                                                                                                                                                                       |    48 / 50 resolved (96%) |                 **49 / 50 (98%)** |  -2.0 percentage points |
| SWE-bench Pro, 10 tasks x 5 reps                                                                                                                                                                                                                                        |     **45 / 50 resolved (90%)** |                   44 / 50 (88%) |  **+2.0 percentage points** |
| Exploration tasks across 7 repos                                                                                                                                                                                                                                        |             **$6.29** | $19.11 |                 **67% cheaper** |                             |
| Telegraphic output: reply prose per turn                                                                                                                                                                                                                                |                  **30 tokens** |                       67 tokens |         **2.7x less prose** |
| Telegraphic Q&A, 20 prompts x 5 reps                                                                                                                                                                                                                                    |              **$4.48** | $8.40 |               **46.7% cheaper** |                             |
| Terminal-Bench 2.1, 89 tasks x 5 reps (445 trials, matched)                                                                                                                                                                                                             |     351 / 445 resolved (78.9%) |                 351 / 445 (78.9%) |    0.0 percentage points (tied) |
| Terminal-Bench fresh input tokens                                                                                                                                                                                                                                       |                        **182K** |                            12.87M |               **98.6% fewer** |
| Terminal-Bench cost (86/89 tasks, normalized to 1-hour cache-write rate)                                                                                                                                                                                                |                        **$61.98** |                            $73.75 |               **16.0% cheaper** |

## SWE-bench Verified

End-to-end bug fixing on 50 SWE-bench Verified instances across 12 Python repos, with 5 reps each. Both arms used the same model, same Docker image, same conda environment, same turn cap, same timeout, and same disabled tools. The LemonCrow arm used `lemoncrow:auto`.


| Arm         |        Cost | Input tok | Cache write |  Cache read | Output tok |  Total tok |     Turns |      Time |       Resolved       |
| ------------- | ------------: | ----------: | ------------: | ------------: | -----------: | -----------: | ----------: | ----------: | :---------------------: |
| **LemonCrow** | **$165.45** | 1,007,977 |   5,730,565 |  97,238,294 |  2,192,112 | **106.2M** | **4,336** | **10.9h** | **232 / 250 (92.8%)** |
| Baseline    |     $234.84 | 1,118,221 |   7,036,456 | 181,596,567 |  3,039,396 |     192.8M |     6,962 |     14.3h |   202 / 250 (80.8%)   |
| Delta       |      -29.5% |     -9.9% |      -18.6% |      -46.5% |     -27.9% |     -44.9% |    -37.7% |    -23.7% |       +12.0 pp       |

Raw data: [`swe50_2026_06_30/`](https://github.com/lemoncrow-lab/benchmarks/tree/main/codebench/results/swe50_2026_06_30)

Run it:

```bash
CODEBENCH_LEMONCROW_AGENT=lemoncrow:auto \
uv run --project benchmarks python -m benchmarks.codebench.multiswe_run \
  --suite swe-bench-verified \
  --instances $(cat benchmarks/codebench/data/verified.txt) \
  --min-changed-files 1 \
  -a baseline lemoncrow \
  --reps 5 \
  --model claude-opus-4-8 \
  --jobs 8
```

### Setup Notes

Every knob below was identical for both arms unless marked LemonCrow-only.

- Model: `claude-opus-4-8`, default sampling.
- Environment: each instance's official SWE-bench Verified Docker image; repo conda env activated identically; agent runs as root (`IS_SANDBOX=1`).
- Reps: 5 per instance.
- Resolved: official `swebench` harness passes the hidden gold tests.
- Turn cap and timeout: `--max-turns 100`; per-run agent timeout 3600 seconds.
- Egress: hermetic except `api.anthropic.com`.
- Disabled tools in both arms: `AskUserQuestion`, `EnterPlanMode`, `ExitPlanMode`, `WebFetch`, `WebSearch`, LemonCrow `web_fetch`, `Workflow`, and `ScheduleWakeup`.
- LemonCrow-only persona: `lemoncrow:auto`.

### Current build spot-check (2026-07-30, 1 rep)

A fresh single-rep LemonCrow run on the current build, against the same 50 instances, re-priced/re-tokenized per-task-average (baseline unchanged, still the 5-rep 2026-06-30 run). **Honest note:** correctness swings from +12.0pp above baseline (5-rep headline) to -4.8pp below it here -- with n=1/task this is exactly the kind of single-rep noise this doc has flagged before (see SWE-bench Pro below), not a claimed regression, but it's reported as measured rather than smoothed over.

| Metric | Baseline (5-rep, unchanged) | LemonCrow (1-rep, 2026-07-30) | Delta |
| --- | ---: | ---: | ---: |
| Cost (per-task avg, summed) | $46.97 | $40.50 | -13.8% |
| Fresh input tok (per-task avg, summed) | 223,644 | 209,255 | -6.4% |
| Cache write (per-task avg, summed) | 1,407,291 | 1,357,474 | -3.5% |
| Cache read (per-task avg, summed) | 36,319,313 | 28,430,274 | -21.7% |
| Output tok (per-task avg, summed) | 607,879 | 534,634 | -12.0% |
| Turns (per-task avg, summed) | 1,392 | 1,149 | -17.5% |
| Resolved | 202 / 250 (80.8%) | 38 / 50 (76.0%) | -4.8 pp |

Raw data: `benchmarks/codebench/results/sweverified_lemoncrow_2026-07-30/` (local only; not yet mirrored to the public [lemoncrow-lab/benchmarks](https://github.com/lemoncrow-lab/benchmarks) repo).

## SWE-bench Lite

A smaller companion cut: 10 SWE-bench Lite instances x 5 reps, same harness (`multiswe_run.py`), same model, same disabled-tools list, and the same `lemoncrow:auto` persona as the Verified run above.


| Arm         |       Cost | Input tok | Cache write |  Cache read | Output tok |  Total tok |   Turns |        Time |     Resolved     |
| ------------- | -----------: | ----------: | ------------: | ------------: | -----------: | -----------: | --------: | ------------: | :-----------------: |
| **LemonCrow** | **$17.51** |   150,236 |     601,817 | 11,582,911 |    197,782 | **12.53M** | **689** | **66.5min** |   48 / 50 (96%)   |
| Baseline    |     $19.83 |   198,203 |     669,766 | 12,180,657 |    251,465 |     13.30M |     771 |     68.8min | **49 / 50 (98%)** |
| Delta       |     -11.7% |    -24.2% |      -10.1% |       -4.9% |     -21.3% |      -5.8% |  -10.6% |       -3.2% |     -2.0 pp     |

Raw data: [`swe-lite_2026-07-16/`](https://github.com/lemoncrow-lab/benchmarks/tree/main/codebench/results/swe-lite_2026-07-16).

Run it:

```bash
CODEBENCH_LEMONCROW_AGENT=lemoncrow:auto \
uv run --project benchmarks python -m benchmarks.codebench.multiswe_run \
  --suite swe-lite \
  --instances astropy__astropy-13579 django__django-12155 django__django-13837 django__django-14007 \
    pallets__flask-5014 psf__requests-6028 pydata__xarray-3305 pydata__xarray-3993 \
    pytest-dev__pytest-8399 sympy__sympy-13877 \
  -a baseline lemoncrow \
  --reps 5 \
  --model claude-opus-4-8 \
  --jobs 3
```

### Current build spot-check (2026-07-30, 1 rep)

Same 10 pinned instances, fresh single-rep LemonCrow run on the current build (baseline unchanged, still the 5-rep 2026-07-16 run):

| Metric | Baseline (5-rep, unchanged) | LemonCrow (1-rep, 2026-07-30) | Delta |
| --- | ---: | ---: | ---: |
| Cost (per-task avg, summed) | $3.97 | $3.18 | -19.9% |
| Fresh input tok (per-task avg, summed) | 39,641 | 40,740 | +2.8% |
| Cache write (per-task avg, summed) | 133,953 | 128,121 | -4.4% |
| Cache read (per-task avg, summed) | 2,436,131 | 1,594,989 | -34.5% |
| Output tok (per-task avg, summed) | 50,293 | 35,722 | -29.0% |
| Turns (per-task avg, summed) | 154 | 110 | -28.7% |
| Resolved | 49 / 50 (98.0%) | 10 / 10 (100.0%) | +2.0 pp |

Fresh input ticks up slightly (n=1/task noise, not a real regression at this size). Raw data: `benchmarks/codebench/results/swe-lite_lemoncrow_2026-07-30/` (local only; not yet mirrored to the public [lemoncrow-lab/benchmarks](https://github.com/lemoncrow-lab/benchmarks) repo).

### September 2026 release validation (1 rep, LemonCrow-only)

A 2026-09-21 hardening run used the same 10 pinned SWE-bench Lite tasks, `claude-opus-4-8`, and official SWE-bench grading. It resolved **10 / 10 tasks**. This is release-validation evidence, not a replacement for the 5-rep A/B headline above: the July spot-check did not record its Claude Code CLI version, and this September debugging run used live bind-mounted LemonCrow source while fixes were still landing. Those provenance gaps make historical cost/turn deltas diagnostic rather than a controlled release-over-release claim. Raw validation artifacts: [`swe-lite-release-validation_2026-09-21/`](https://github.com/lemoncrow-lab/benchmarks/tree/main/codebench/results/swe-lite-release-validation_2026-09-21).

| Metric | 2026-09-21 validation |
| --- | ---: |
| Resolved | **10 / 10 (100%)** |
| Cost | $3.2270 |
| Fresh input tok | 39,681 |
| Cache write | 116,941 |
| Cache read | 1,864,867 |
| Output tok | 37,069 |
| Turns | 112 |
| Wall time | 602.5s |

During the audit this run exposed and led to fixes for model-facing `structuredContent` duplication, incomplete symbol hydration, ranged-edit anchor handling, and multi-symbol code-search ranking. A post-ranking-fix two-task smoke (`django__django-14007` + `pallets__flask-5014`) resolved **2 / 2** at $0.6597 total; Django dropped from $0.7390 / 25 turns in the earlier validation trajectory to $0.5180 / 20 turns after the ranking fix. Raw smoke artifacts: [`swe-lite-ranking-proof_2026-09-21/`](https://github.com/lemoncrow-lab/benchmarks/tree/main/codebench/results/swe-lite-ranking-proof_2026-09-21).

Starting with the next publishable run, CodeBench pins **Claude Code 2.1.197** in the overlay and writes `benchmark-manifest.json` with the Claude Code version, LemonCrow commit, task list, model, and start/end runtime-source fingerprints. A run whose runtime fingerprint changes while it is executing is explicitly marked non-publishable in `report.txt`.

#### Cheap release gates

**2026-09-21 release status:** the full 10-task SWE-bench Lite validation is already complete (10/10), and the post-ranking-fix Django/Flask smoke is already complete (2/2). **Do not rerun either for this release.** Use the remaining cheap gates below only to add coverage on dimensions the SWE run did not exercise.

```bash
# $0 model spend: retrieval quality/latency against the frozen retrieval corpus.
# Best next gate after search/index/ranking changes.
uv run lemoncrow eval retrieval --channel lexical --full --resume --csv /tmp/retrieval_mrr.csv

# ~ $0.20 historically: cross-language exploration smoke (Go + Python + Rust).
# Adds breadth beyond the Python-heavy SWE-Lite slice.
uv run --project benchmarks python -m benchmarks.codebench.run \
  cg_gin cg_django cg_tokio \
  -a lemoncrow --reps 1 --model claude-opus-4-8 --jobs 2
```

For cache/RTK/Headroom changes, use the existing Harbor `fix-code-vulnerability` warm-cache comparison rather than rerunning SWE-Lite. The dollar figures above are observations, not budgets or guarantees. A new full SWE-Lite run belongs to the **next release or a materially different runtime**, not this one.

## SWE-bench Pro

A structurally different, harder benchmark than SWE-bench (Verified/Lite above): [SWE-bench Pro](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro) (ScaleAI) covers non-Python-heavy, often larger production codebases -- Go, TypeScript/JS, Python across vuls, flipt, element-web, qutebrowser (x2), tutanota, navidrome, NodeBB, teleport, and openlibrary -- graded by ScaleAI's own harness (`scaleapi/SWE-bench_Pro-os`), not the `swebench` package. The pinned default 10-instance slice, 5 reps per arm (50 runs a side), `claude-opus-4-8`, same disabled-tools list and `lemoncrow:auto` persona as the runs above. The suite's one dead instance (protonmail/webclients -- base image can't build) was dropped from the default slice entirely, pulling in a previously-unrun 10th task in its place.


| Arm         |       Cost | Input tok | Cache write | Cache read | Output tok | Total tok |   Turns |     Time |     Resolved     |
| ------------- | -----------: | ----------: | ------------: | -----------: | -----------: | ----------: | --------: | ---------: | :-----------------: |
| **LemonCrow** | **$30.61** |   160,678 |   1,092,763 | 22,395,637 |    307,214 | **24.0M** | **999** | **2.0h** | **45 / 50 (90%)** |
| Baseline    |     $39.01 |   271,650 |   1,518,457 | 34,821,434 |    446,755 |     37.1M |   1,390 |     2.4h |   44 / 50 (88%)   |
| Delta       |     -21.5% |    -40.9% |      -28.0% |     -35.7% |     -31.2% |    -35.4% |  -28.1% |   -17.3% |      +2.0 pp      |


| Task (repo)                      | Language | LemonCrow                      | Baseline |
| ---------------------------------- | ---------- | ------------------------------ | ---------- |
| future-architect/vuls            | Go       | 5/5, $1.44  | 5/5, $1.12     |          |
| flipt-io/flipt                   | Go       | 5/5, $3.09 | 5/5, $2.39      |          |
| element-hq/element-web           | TS/JS    | 5/5, $3.77 | 5/5, $4.32      |          |
| qutebrowser/qutebrowser-0833b5f6 | Python   | 5/5, $0.34  | 5/5, $0.55     |          |
| qutebrowser/qutebrowser-c09e1439 | Python   | 5/5, $2.99 | 5/5, $5.18      |          |
| tutao/tutanota                   | TS/JS    | 5/5, $4.33 | 5/5, $3.65      |          |
| navidrome/navidrome              | Go       | 5/5, $2.55 | 5/5, $2.60      |          |
| NodeBB/NodeBB                    | JS       | **1/5**, $6.92 | 3/5, $11.12 |          |
| gravitational/teleport           | Go       | 5/5, $4.45 |**1/5**, $6.40   |          |
| internetarchive/openlibrary      | Python   | **4/5**, $0.72  | 5/5, $1.69 |          |

<sub>Cells: reps resolved out of 5, then the 5-rep total cost for that arm.</sub>

Honest result: at 5 reps the earlier single-rep correctness loss disappears -- LemonCrow resolves 45/50 vs baseline's 44/50 (+2.0 pp) and is 21.5% cheaper end-to-end. The correctness deltas concentrate in 3 tasks (teleport 5/5 vs baseline 1/5; NodeBB 1/5 vs baseline 3/5; openlibrary 4/5 vs 5/5) -- every other task ties 5/5. Three tasks (flipt, vuls, tutanota) cost more than baseline despite matching correctness, a known tradeoff on larger non-Python codebases. This 5-rep run supersedes the earlier single-rep cut's -10.0pp result, which was n=1 noise.

Raw data: [`swe-pro_2026_07_07/`](https://github.com/lemoncrow-lab/benchmarks/tree/main/codebench/results/swe-pro_2026_07_07) -- the original single-invocation 2026-07-06 rep1 (with the protonmail dead slot) is kept at [`swe-pro_2026-07-06/`](https://github.com/lemoncrow-lab/benchmarks/tree/main/codebench/results/swe-pro_2026-07-06) for history.

Run it:

```bash
CODEBENCH_LEMONCROW_AGENT=lemoncrow:auto \
uv run --project benchmarks python -m benchmarks.codebench.multiswe_run \
  --suite swe-pro \
  --limit 10 \
  -a baseline lemoncrow \
  --reps 5 \
  --model claude-opus-4-8 \
  --jobs-per-token 4
```

### Current build spot-check (2026-07-30, 1 rep)

`--suite swe-pro`'s pinned default slice has grown from 10 to 18 instances since the baseline run above (8 new tasks, none with baseline data yet). This spot-check is filtered back down to the original 10 overlapping instances for a fair comparison; the current build's other 8 tasks aren't included below.

| Metric | Baseline (5-rep, unchanged) | LemonCrow (1-rep, 2026-07-30) | Delta |
| --- | ---: | ---: | ---: |
| Cost (per-task avg, summed) | $7.80 | $5.79 | -25.7% |
| Input tok (per-task avg, summed) | 54,330 | 43,216 | -20.5% |
| Cache write (per-task avg, summed) | 303,691 | 229,713 | -24.4% |
| Cache read (per-task avg, summed) | 6,964,287 | 3,857,422 | -44.6% |
| Output tok (per-task avg, summed) | 89,351 | 54,095 | -39.5% |
| Turns (per-task avg, summed) | 278 | 169 | -39.2% |
| Resolved | 44 / 50 (88.0%) | 10 / 10 (100.0%) | +12.0 pp |

Raw data: `benchmarks/codebench/results/swe-pro_lemoncrow_2026-07-30/` (18 tasks total, local only; not yet mirrored to the public [lemoncrow-lab/benchmarks](https://github.com/lemoncrow-lab/benchmarks) repo).

## Exploration Tasks

7 open-source codebases, 1 exploration question each, 5 reps per arm, `claude-opus-4-8`. Costs are summed across all reps. The baseline arm is the 2026-06-29 run; the LemonCrow arm was re-run on 2026-07-08 against the current runtime; a third arm -- [CodeGraph](https://github.com/colbymchenry/codegraph), wired in via the harness's BYO-competitor path against its MCP server -- was added on 2026-07-21. Same tasks, prompts, model, timeout, and driver throughout (protocol recorded in the run's `benchmark-manifest.json`).


| Codebase   | Language / size                   |                LemonCrow |        Baseline |          Codegraph |     LemonCrow Δ |      Codegraph Δ |
| ------------ | ----------------------------------- | -----------------------: | ----------------: | --------------------: | -----------------: | -----------------: |
| Tokio      | Rust, 784 files, 176k lines       |          $0.34 | $2.69 |               $3.44 | **87% cheaper** |       28% pricier |
| Alamofire  | Swift, 98 files, 44k lines        |          $0.74 | $4.83 |               $2.48 | **85% cheaper** |   **49% cheaper** |
| Django     | Python, 3k files, 522k lines      |          $0.37 | $2.31 |               $2.32 | **84% cheaper** |               even |
| OkHttp     | Java, 596 files, 133k lines       |          $0.29 | $1.60 |               $1.35 | **82% cheaper** |       16% cheaper |
| VS Code    | TypeScript, 11k files, 3.3M lines |          $0.72 | $3.08 |               $2.56 | **77% cheaper** |       17% cheaper |
| Gin        | Go, 99 files, 24k lines           |          $0.29 | $1.09 |               $1.36 | **73% cheaper** |       25% pricier |
| Excalidraw | TypeScript, 600 files, 171k lines |          $3.54 | $3.51 |               $2.47 |    +0.7% (even) |   **30% cheaper** |
| **Total**  | 7 repos, 16k files, 4.4M lines    | **$6.29** | **$19.11** | **$15.99** | **67% cheaper** | **16% cheaper** |

Honest outlier: Excalidraw is a dead heat for LemonCrow ($3.54 vs $3.51) -- the one repo where its answer style spends as much as it saves. Every other repo is 73-87% cheaper for LemonCrow. Codegraph is noisier: cheaper on 5 of 7 repos (16-49%; Excalidraw is its best result, LemonCrow's worst), but pricier than baseline on Tokio (+28%) and Gin (+25%), and 22.7% slower overall (1,653,761ms vs baseline's 1,347,619ms) despite netting 16.3% cheaper in total cost -- a different picture than CodeGraph's own published with/without numbers on this same 7-repo set, which show it uniformly faster and never pricier. Beyond cost: LemonCrow cuts turns 91% (1,237 → 112) and codegraph 84% (→ 197); cache-read tokens fall 92% for LemonCrow vs 89% for codegraph; output tokens fall 84% for LemonCrow vs 77% for codegraph (→ 99,043).

Raw data: [`exploration_2026_06_29/`](https://github.com/lemoncrow-lab/benchmarks/tree/main/codebench/results/exploration_2026_06_29)

Run it:

```bash
lc benchmark codebench \
  --arm baseline --arm lemoncrow \
  --task cg_vscode --task cg_excalidraw --task cg_django --task cg_tokio \
  --task cg_okhttp --task cg_gin --task cg_alamofire \
  --reps 5 \
  --model claude-opus-4-8 \
  --cli-driver claude
```

The codegraph arm isn't yet wired through the `lc benchmark codebench` wrapper's `--arm`; it runs through the harness's BYO-competitor path directly:

```bash
uv run python -m benchmarks.codebench.run \
  cg_vscode cg_excalidraw cg_django cg_tokio cg_okhttp cg_gin cg_alamofire \
  --arms codegraph \
  --competitor benchmarks/codebench/competitors/codegraph.json \
  --reps 5 --model claude-opus-4-8 --timeout 1800 \
  --jobs 1 --parallel-scope task \
  --out benchmarks/codebench/results/exploration_2026_06_29 --resume
```

## Telegraphic Q&A Benchmark

20 general engineering Q&A prompts (React re-renders, connection pooling, git rebase vs merge, race conditions, error boundaries, ...; no code repo, no golden patch -- these are explanation prompts, not bug fixes). Three arms in one run: baseline (vanilla Claude Code), `lemoncrow:auto` through the full plugin+MCP runtime, and **caveman** (`benchmarks/telegraphic/caveman_skill.md` appended as the only system prompt, no plugin/tooling/MCP -- the free "just tell Claude to be terse" DIY alternative anyone can paste into their own CLAUDE.md today). `claude-opus-4-8`, 5 reps per prompt per arm (300 runs total), `--max-turns 50`.


| Arm                        |      Cost | Input tok | Cache write | Cache read | Output tok | Total tok |    Turns |        Time |
| ---------------------------- | ----------: | ----------: | ------------: | -----------: | -----------: | ----------: | ---------: | ------------: |
| **LemonCrow**                | **$4.48** |   325,740 |     110,471 |  1,166,920 | **46,444** |     1.65M |  **143** | **22.1min** |
| Baseline                   |     $8.40 |   350,505 |     275,312 |  2,338,476 |    118,654 | **3.08M** |      240 |     33.8min |
| Caveman                     |     $8.67 |   351,255 |     445,399 |  1,947,520 |     69,481 |     2.81M |      221 |     21.9min |
| Delta, LemonCrow vs baseline |    -46.7% |     -7.1% |      -59.9% |     -50.1% |     -60.9% |     -46.5% | -40.4%† |      -34.5% |

Every metric moves in LemonCrow's favor this run: cost falls 46.7%, output tokens fall 60.9%, and total token volume falls 46.5% -- input, cache write, and cache read all shrink too, so there's no cache-read/token-count anomaly to explain away like the prior cut. Raw turns fall 40.4%; see the title-generation correction below for the more apples-to-apples comparison.

† Baseline and caveman each pay a hidden Claude Code session-title API call that LemonCrow's `--agent` invocation fully suppresses (0/100 lemoncrow runs trigger it, vs 98/100 baseline and 100/100 caveman runs). Corrected for it, real answering turns per prompt are baseline **1.42**, caveman **1.21**, lemoncrow **1.43** -- once the title-generation round trip both other arms pay is stripped out, LemonCrow's real turn count is essentially tied with baseline and still higher than caveman's.

Per-prompt output tokens (median across 5 reps; final average is the mean of the 20 prompt medians):


| Prompt                              |  Baseline | LemonCrow | Caveman |
| ------------------------------------- | ----------: | --------: | ------: |
| React re-render (object prop)       |       873 |     254 |     268 |
| Express JWT expiry bug              |     1,926 |     492 |     694 |
| Postgres connection pool setup      |     1,898 |     791 |   1,044 |
| git rebase vs merge                 |     1,044 |     452 |     675 |
| Callback -> async/await refactor    |       553 |     133 |     393 |
| Split a monolith into microservices |     1,461 |     646 |     997 |
| PR security review                  |     1,034 |     244 |     569 |
| Multi-stage Dockerfile              |     1,419 |     885 |     692 |
| Postgres counter race condition     |     1,277 |     369 |     641 |
| React error boundary component      |     2,649 |   1,246 |   2,946 |
| 10 caveman-style eval prompts (avg) |       936 |     279 |     421 |
| **Average, all 20**                 | **1,175** | **415** | **657** |

LemonCrow vs baseline, output tokens per prompt: down on **20 of 20** (mean 67%, median 70%, range 38%-84%, stdev 10pp). No regression this run -- even `error-boundary` (a genuine code-generation answer, not prose) falls 53% (2,649 -> 1,246).

Caveman vs baseline, output tokens per prompt: down on 19 of 20 (mean 48%, median 50%, range -11%-82%, stdev 18pp). `error-boundary` is caveman's own regression this run (2,946 vs baseline's 2,649, +11.2% more tokens) -- the same genuine code-generation answer where LemonCrow no longer regresses. Caveman also now costs **3.3% more** than baseline overall (not less, as in the prior cut) despite the token cut, driven by far heavier cache-write spend -- it only compresses replies, not the input/context tokens that set the price.

Raw data: [`telegraphic_2026_07_17/`](https://github.com/lemoncrow-lab/benchmarks/tree/main/codebench/results/telegraphic_2026_07_17) -- includes `summary.csv`, the full `results.jsonl` (300 rows: baseline/lemoncrow/caveman x 20 prompts x 5 reps), and per-call `.flow_dump.txt` transcripts (raw `.flow` wire captures are gitignored; they carry bearer tokens).

Run it:

```bash
uv run lemoncrow benchmark telegraphic \
  --arm baseline --arm lemoncrow --arm caveman \
  --model claude-opus-4-8 \
  --reps 5 \
  --max-turns 50 \
  --jobs 4 \
  -y
```

baseline/lemoncrow run through codebench with `--jobs`; caveman's isolated `claude -p` calls always run one at a time, by design (`benchmarks/telegraphic/extra_arms.py`).

### Current build spot-check (2026-07-30, 1 rep)

Same 20 prompts, fresh single-rep LemonCrow run on the current build (baseline unchanged, still the 5-rep 2026-07-17 run):

| Metric | Baseline (5-rep, unchanged) | LemonCrow (1-rep, 2026-07-30) | Delta |
| --- | ---: | ---: | ---: |
| Cost (per-prompt avg, summed) | $1.68 | $1.06 | -36.8% |
| Fresh input tok (per-prompt avg, summed) | 70,101 | 50 | -99.9% |
| Cache write (per-prompt avg, summed) | 55,062 | 75,967 | +38.0% |
| Cache read (per-prompt avg, summed) | 467,695 | 155,201 | -66.8% |
| Output tok (per-prompt avg, summed) | 23,731 | 8,957 | -62.3% |
| Turns (per-prompt avg, summed) | 48 | 25 | -47.9% |

Cache write is the one metric that regressed (+38.0%) -- worth another look if it persists on a repeat run. No golden patch here, so no resolved/correctness row. Raw data: local only (scratch-repo run, not yet copied into the repo or mirrored to the public [lemoncrow-lab/benchmarks](https://github.com/lemoncrow-lab/benchmarks) repo).


### Readable-ultra compression experiment (2026-09-14, Claude Opus 5)

Goal: keep the **same practical compression as legacy `ultra`** while removing the decoding cost caused by fragment-heavy prose and `→`/slash/semicolon chains. The experiment used the same 20 telegraphic Q&A prompts, the same `lemoncrow:solve` persona, plugin, MCP runtime, and tools. Only the reply-register text changed. Each candidate was run at 1 rep on `claude-opus-5`; previously completed arms were reused rather than rerun.

For this experiment, **visible reply tokens** means Claude's reported output tokens minus hidden thinking tokens from `model_usage`. The benchmark's top-level `thinking_tokens` field was zero for these runs, so using raw output alone overstates what the user actually reads. Single-rep numbers are directional rather than a statistical claim.

| Variant | Reply contract tested | Median visible | Avg visible | Median output | Avg output | Avg cost / prompt | Outcome |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| **v1 — legacy ultra** | `≤3 lines / ≤50 words`; forced `done|blocked: … → risk → verified: …`; fragments over prose; one fix only | **152.5** | 437.6† | 211.0 | 527.1† | $0.0606 | Compression target, but hard to read |
| **v2 — readable prose** | Complete sentences; no punctuation-coded reasoning; normally `≤80` prose words / 2–4 sentences; smallest code; one tradeoff | 194.5 | **279.2** | 286.5 | 393.8 | **$0.0516** | Very readable; typical answer ~28% larger than v1 |
| **v3 — controlled shorthand** | Natural status fragments; labels/`+` allowed; one causal relation per sentence; normally `≤60` words | 227.0 | 294.2 | 303.5 | 413.3 | $0.0525 | Readable, but shorthand did not improve compression |
| **v4 — information slots** | Max three slots: answer/cause, fix/decision, verification/risk; normally `≤45` words; minimal code; bounded bullets | 209.0 | 300.4 | 239.0 | 374.6 | $0.0559 | Closer, but model often treated the word budget as soft |
| **v5 — legacy scope + readable syntax** | Restored v1's hard `≤3 lines / ≤50 words`, one-fix scope, and answer-then-stop behavior; replaced fragment/arrow encoding with short readable English and `Tests:` / `Risk:` labels | **140.0** | **270.4** | **166.0** | **351.5** | $0.0542 | **Winner; promoted to production `ultra`** |

† Legacy ultra had two code-heavy runaway answers (`Dockerfile`: 2,506 output tokens; `error-boundary`: 3,806), so its averages are much worse than its typical median. That is why the promotion criterion was primarily **median visible size + readability**, with average/cost as secondary checks.

What changed across the iterations:

- **v1 / legacy ultra:** compression partly came from removing grammar. It achieved a 152.5-token median visible reply, but instructions such as “fragments over prose” and the mandatory arrow-form task report made normal status answers unnecessarily difficult to parse.
- **v2:** tested the hypothesis “compress information, not English.” Readability improved immediately and average output improved because scope was tighter, but the typical answer grew to 194.5 visible tokens.
- **v3:** reintroduced controlled shorthand to recover the gap. It allowed natural fragments and compact labels while banning causal arrow chains. The model used the saved grammar budget to add more information, so median visible output worsened to 227 tokens.
- **v4:** constrained *information count* instead: at most three semantic slots and a nominal 45-word budget. It reached 209 visible tokens, but multi-factor/comparison prompts frequently exceeded the nominal budget.
- **v5:** stopped redesigning ultra and changed only the readability mechanism. It reused legacy ultra's proven hard cap and strict scope, but required readable syntax. Median visible output reached **140 tokens — 8.2% smaller than legacy ultra** — without the fragment/arrow decoding burden. This exact contract replaced the production `ultra` register.

Representative production contract after promotion:

```text
Hard cap ≤3 lines / ≤50 words.
Open on the result; answer only what was asked; one applicable fix.
Keep result/cause + fix/implication + material verification/risk.
Use short readable English. Fragments only for clear labels such as Tests: or Risk:.
Never encode reasoning with →, slash chains, semicolon piles, or dense noun stacks.
```

Runs and tuning notes:

| Variant | Run | Notes |
| --- | --- | --- |
| v1 + v2 + lite reference | `/tmp/lemoncrow-telegraphic-scratch-repo/reports/benchmark/telegraphic/20260914T080433Z` | Full 20-prompt comparison. v2 was tuned first on a 5-prompt smoke before this full run. |
| v3 | `/tmp/lemoncrow-telegraphic-scratch-repo/reports/benchmark/telegraphic/20260914T084447Z` | v3-only run; two empty CLI payloads were retried, not counted as model failures. |
| v4 | `/tmp/lemoncrow-telegraphic-scratch-repo/reports/benchmark/telegraphic/20260914T090818Z` | v4-only run. Keyword-overlap validator marked the concise debounce answer invalid even though manual inspection found it on-topic. |
| v5 | `/tmp/lemoncrow-telegraphic-scratch-repo/reports/benchmark/telegraphic/20260914T091116Z` | v5-only run; one empty CLI payload was retried. Same keyword-overlap heuristic false-negative occurred on the debounce answer. |

The v2–v5 registers and CLI arms were deliberately removed after the experiment. They were temporary research variants, not user-facing settings. The permanent public levels remain `ultra`, `lite`, and `off`; `lemoncrow-readable` remains as the benchmark arm that selects public `lite` for an apples-to-apples style comparison.


## Retrieval — local code-search quality

LemonCrow local uses the **lexical** retrieval path. Zoekt and semantic retrieval are hosted capabilities and are intentionally excluded from the local benchmark headline.

### Current local release result — 2026-09-21

The current lexical-only release sweep covers 14 repositories and 6,292 scored gold cases across definition, content, semantic-intent, SWE-bench, and session-derived queries. The benchmark uses populated frozen repository indexes, forces each MRR query to be independent (`code_search(force=true)`), and fails closed if a routed snapshot is missing or empty.

| Metric | LemonCrow local lexical |
| --- | ---: |
| Overall MRR | **0.6425** |
| hit@1 | **0.5701** |
| hit@3 | **0.7009** |
| p95 latency | **141 ms** |
| Queries | **6,292** |
| Definition MRR | **0.8658** |
| Content MRR | **0.8732** |
| SWE-bench MRR | **0.4981** |
| Session MRR | **0.5581** |

Representative definition MRR by repository: Django **0.8424**, Astropy **0.9550**, Requests **0.9390**, Xarray **0.9350**, Pytest **0.9850**, SymPy **0.7475**, Linux **0.9222**, and LemonCrow **0.7840**.

Raw release data and per-repo details: [`retrieval_local_lexical_2026-09-21/`](https://github.com/lemoncrow-lab/benchmarks/tree/main/codebench/results/retrieval_local_lexical_2026-09-21).

The current gold corpus is not identical to the older 7,213-pair competitor corpus below, so **0.6425 must not be compared directly with the historical provider scores**. The current result is the release/regression number for local LemonCrow; the table below remains the matched historical cross-tool comparison.

### Historical matched competitor comparison

This July comparison scored the local lexical path and named third-party tools on the same 14 repositories and the same 7,213 query/gold pairs. Hosted-only LemonCrow retrieval modes are omitted here so the product surface matches the local benchmark claim.

| Provider | MRR | rec@1 | rec@2 | rec@3 | p95 | p100 | n |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ⭐ LemonCrow local lexical | **0.676** | **0.582** | **0.700** | **0.743** | 134ms | 319ms | 7213 |
| cocoindex-code | 0.557 | 0.457 | 0.567 | 0.625 | 595ms | 2061ms | 7213 |
| Graft 0.8.2 | 0.514 | 0.433 | 0.521 | 0.566 | 1770ms | 2759ms | 7213 |
| codebase-memory-mcp | 0.502 | 0.437 | 0.511 | 0.553 | 541ms | 1817ms | 7213 |
| fff-mcp | 0.430 | 0.388 | 0.434 | 0.456 | 46ms | 207ms | 7213 |
| serena | 0.401 | 0.359 | 0.405 | 0.424 | 3834ms | 269001ms | 7213 |
| ripgrep | 0.376 | 0.320 | 0.376 | 0.405 | 66ms | 522ms | 7213 |
| code-index-mcp | 0.343 | 0.296 | 0.345 | 0.371 | 377ms | 3830ms | 7213 |
| ast-grep | 0.312 | 0.271 | 0.317 | 0.341 | 1255ms | 8806ms | 7213 |
| jcodemunch-mcp | 0.299 | 0.226 | 0.289 | 0.341 | 214ms | 4189ms | 7213 |
| codegraph | 0.296 | 0.267 | 0.299 | 0.316 | 17ms | 532ms | 7213 |
| universal-ctags | 0.237 | 0.226 | 0.242 | 0.245 | **1ms** | **12ms** | 7213 |

The July LemonCrow lexical row is a re-run after a latency fix (an unbounded ANN-matrix cache-miss path) and a harness measurement bug (the bench server was paying its own statusline pipeline inside timed queries); other providers' latency numbers predate that fix and may be pessimistic. The Graft row is a 2026-08-03 run of pinned `@nanonets/graft@0.8.2` through its shipped persistent MCP server.

Historical matched raw data: [`retrieval_2026_07_05/`](https://github.com/lemoncrow-lab/benchmarks/tree/main/codebench/results/retrieval_2026_07_05).

Run the current local benchmark:

```bash
uv run lemoncrow eval retrieval --channel lexical --full --resume --csv /tmp/retrieval_mrr.csv
```

## Hosted retrieval indexing time

Zoekt and semantic indexing are hosted capabilities; this section is separate from the local lexical benchmark above.


Cold full rebuild time per phase.


| Repo         |   Symbols | Lexical only | Zoekt only | Semantic only, BGE-Code-v1 |
| -------------- | ----------: | -------------: | -----------: | ---------------------------: |
| requests     |     1,133 |        2.22s |      0.11s |                      1.62s |
| flask        |     1,354 |        2.19s |      0.11s |                      1.35s |
| seaborn      |     3,167 |        3.17s |      0.30s |                      3.08s |
| pytest       |     4,250 |        2.99s |      0.33s |                      4.16s |
| xarray       |     5,276 |        4.51s |      0.26s |                      5.05s |
| pylint       |    11,770 |        4.73s |      0.44s |                     11.37s |
| sphinx       |    12,223 |        7.27s |      0.67s |                     19.00s |
| scikit-learn |    13,227 |       10.35s |      0.62s |                     18.94s |
| lemoncrow      |    23,565 |       11.99s |      2.97s |                     26.67s |
| sympy        |    24,112 |       19.68s |      1.05s |                     20.94s |
| matplotlib   |    31,384 |       12.63s |      1.68s |                     28.30s |
| django       |    38,931 |       21.91s |      1.31s |                     45.14s |
| astropy      |    40,198 |       16.82s |      2.28s |                     37.01s |
| linux        | 1,239,077 |      179.49s |     13.69s |                  1,208.89s |

Commands:

```bash
lc code index --reindex
LEMONCROW_ZOEKT_MODE=installed lc code index --reindex
LEMONCROW_ZOEKT_MODE=installed LEMONCROW_CODE_EMBEDDER=bge lc code index --reindex
```

## Semantic Code Search Embedder Sweep

LemonCrow ships BGE-Code-v1 as the default semantic embedder. It had the best average MRR in the corrected sweep and indexes faster than the next-closest larger model. On CPU or GPUs below the VRAM threshold, LemonCrow falls back to SFR-Embedding-Code-400M_R.


| Model                   | Params |   Def MRR | Content MRR | Semantic MRR |       Avg |
| ------------------------- | -------: | ----------: | ------------: | -------------: | ----------: |
| **BGE-Code-v1**         |  ~1.5B | **0.828** |   **0.835** |    **0.879** | **0.847** |
| GTE-Qwen2-1.5B          |  ~1.5B |     0.771 |       0.812 |        0.767 |     0.783 |
| Nomic-embed-code 3584d  |    ~7B |     0.756 |       0.798 |        0.755 |     0.770 |
| Nomic-embed-code 768d   |    ~7B |     0.746 |       0.785 |        0.746 |     0.759 |
| SFR-Embedding-Code-400M |   400M |     0.738 |       0.791 |        0.742 |     0.757 |
| Qwen3-Embedding-0.6B    |   600M |     0.728 |       0.776 |        0.727 |     0.744 |
| Qwen3-Embedding-4B      |    ~4B |     0.724 |       0.775 |        0.726 |     0.742 |
| BGE-M3                  |   570M |     0.684 |       0.746 |        0.704 |     0.711 |
| Arctic-Embed-L-v2       |   568M |     0.639 |       0.704 |        0.663 |     0.669 |

Run the sweep:

```bash
python3 benchmarks/codebench/run_embedder_sweep.py
```

## Terminal-Bench

Agentic terminal tasks on Terminal-Bench 2.1 through the Harbor harness, `claude-opus-4-8`. This is a **matched 5-rep comparison**: LemonCrow ran the full suite at 89 tasks x 5 reps = 445 trials, scored against an official Claude Code 2.1.205 / Opus 4.8 leaderboard run on the same dataset, also 89 tasks x 5 reps = 445 trials (scraped from Harbor Hub; methodology in `benchmarks/harbor/results/baseline/README.md`). Both arms are the same size, so correctness is directly comparable. This run is public: [Harbor Hub job `47e1713b`](https://hub.harborframework.com/jobs/47e1713b-cad9-4715-a9e7-ca71ff202ba7); it supersedes the earlier 356/445 (80.0%) cut, whose +1.1pp edge doesn't reproduce here.


| Arm          |          Resolved | Fresh input tok | Output tok |  Cache tok | Total tok |
| ------------ | -----------------: | --------------: | ---------: | ---------: | --------: |
| **LemonCrow** |  351 / 445 (78.9%) |       **182K** |  **5.36M** | **122.0M** | **127.6M** |
| Baseline     |  351 / 445 (78.9%) |          12.87M |      8.09M |     161.9M |     182.9M |
| Delta        | 0.0 pp (tied) |     **-98.6%** | **-33.8%** | **-24.6%** | **-30.2%** |

Correctness ties baseline exactly this run (351/445 both sides) -- the earlier +1.1pp edge was noise across runs, not a stable win. Token efficiency still holds and improves: **98.6% fewer fresh input tokens** (182K vs 12.87M) and 33.8% fewer output tokens, and this time cache and total tokens land *below* baseline too (-24.6% / -30.2%), reversing the earlier cut's cache/total overshoot. Pass@k under repetition: pass@1 78.9%, pass@2 84.0%, pass@4 87.2%, pass@5 87.6% (30 of LemonCrow's 445 trials errored before completing, vs 34 baseline trials that errored without a billed cost).

### Cost: normalized to one cache-write rate

Raw `cost_usd` on each side isn't apples-to-apples by itself: LemonCrow's harness bills prompt-cache writes at the **1-hour TTL rate** (2x base input, $10/MTok); the baseline's official leaderboard run bills entirely at the cheaper **5-minute TTL rate** (1.25x base input, $6.25/MTok). So baseline is re-priced at LemonCrow's own 1-hour rate for a same-tier comparison -- confirmed sound by recomputing each side's own trials at its real tier and diffing against its reported cost (LemonCrow 1.0043x, baseline 1.0222x -- both within tolerance; see `benchmarks/harbor/normalized_token_cost.py`). Scope: the 86 of 89 tasks where LemonCrow produced at least one priceable trajectory (`extract-moves-from-video`, `gpt2-codegolf`, `make-doom-for-mips` timed out on every rep -- no trajectory to price, though all three still count as failures in the Resolved numbers above):

| Cut                                    |  LemonCrow |   Baseline |             Delta |
| ------------------------------------------- | ------------: | ------------: | --------------------: |
| **Normalized, both @ 1-hour cache-write rate** | **$61.98** |     $73.75 |  **16.0% cheaper** |

LemonCrow's token efficiency is worth **16.0% lower cost** once both sides are priced on the same cache-write tier.

<sub>Fresh input = input tokens excluding cache. Harbor Hub reports baseline input inclusive of cache, so baseline fresh input here is total input minus cache. Cache tokens are combined read + write. Cost figures are per-task averages summed across the 86 comparable tasks (each task weighted equally regardless of billed-rep count), not raw run totals -- see `benchmarks/harbor/normalized_token_cost.py` to reproduce (also reports the un-normalized real-$-per-own-tier and 5-min-tier cuts, for reference).</sub>

Raw data: [`2026-07-28__19-19-14/`](https://github.com/lemoncrow-lab/benchmarks/tree/main/harbor/results/2026-07-28__19-19-14) (445 trials; also public on [Harbor Hub](https://hub.harborframework.com/jobs/47e1713b-cad9-4715-a9e7-ca71ff202ba7)). Baseline + full methodology, including the `_1h_tier` normalized-cost columns: [`baseline/`](https://github.com/lemoncrow-lab/benchmarks/tree/main/harbor/results/baseline).

### Opus 5 (no same-model baseline yet)

Same 89-task x 5-rep suite, run on `claude-opus-5` (`reasoning_effort=high`) on 2026-07-29: public [Harbor Hub job `18239ddc`](https://hub.harborframework.com/jobs/18239ddc-556a-4631-a20d-bcf5da8d16a2), submitted as [leaderboard PR #183](https://github.com/harbor-framework/terminal-bench-2-1/pull/183). No official Claude-Code + Opus-5 baseline exists on the leaderboard yet -- the only other public Opus 5 entry is a different harness ([Ouroboros, PR #175](https://github.com/harbor-framework/terminal-bench-2-1/pull/175), also unmerged) -- so these numbers are reported standalone, **not** as a delta against the Opus 4.8 baseline above (different model = not a controlled comparison; don't subtract these tables).

| Metric | Value |
| --- | --- |
| Resolved | 359 / 445 (80.7%) |
| Errored before completing | 41 / 445 (23 timeout, 18 provider error: 14 safety refusal on 3 tasks + 4 transient 529) |
| Cost (86/89 priceable tasks, per-task avg, summed) | $38.68 |
| Fresh input tokens | 184K |
| Output tokens | 5.17M |
| Cache tokens | 146.0M |
| Total tokens | 151.3M |

<sub>Same per-task-average cost convention as above (`extract-moves-from-video` and `make-doom-for-mips` priced fine this run; `gpt2-codegolf`, `schemelike-metacircular-eval`, and `train-fasttext` didn't -- a different 3-task exclusion set than the Opus 4.8 cut, so the two $-figures aren't the same 86 tasks either). Cost is real, LemonCrow's-own-tier billing (1-hour cache-write rate) -- nothing to normalize against without a same-model baseline.</sub>

Raw data: [`2026-07-29__15-35-08/`](https://github.com/lemoncrow-lab/benchmarks/tree/main/harbor/results/2026-07-29__15-35-08) (445 trials).

Run it:

```bash
lc benchmark harbor -y
```

Useful variants:

```bash
lc benchmark harbor --baseline -y
lc benchmark harbor --limit 3 --attempts 1 -y
lc benchmark harbor --resume benchmarks/jobs/harbor/2026-07-01__12-00-00 -y
```

## Overall Assessment

- **Cost/tokens/turns: LemonCrow wins on every suite measured.** Verified -29.5% cost/-44.9% tokens/-37.7% turns, Lite -11.7%/-5.8%/-10.6%, Pro -21.5%/-35.4%/-28.1%, Exploration -67% cost, Telegraphic Q&A -46.7% cost/-60.9% output tokens (every token category fell this run, cost and tokens agree), Terminal-Bench -16.0% cost (normalized to a matched cache-write rate), -98.6% fresh input tokens. Caveman (free DIY "be terse" system-prompt instruction, no plugin/tooling) cuts output by 41.4% but now costs **3.3% more** than baseline -- it only compresses replies, not the input/context tokens that drive the bill.
- **Correctness wins on most multi-rep suites, ties on one.** Verified +12.0pp, Pro +2.0pp (the 5-rep run overturned an earlier single-rep -10.0pp result -- that loss was n=1 noise), Lite -2.0pp. Terminal-Bench ties baseline exactly on this run (351/445 vs 351/445) -- the earlier +1.1pp edge didn't reproduce.
- **Where overhead still shows up:** non-Python/larger/more heterogeneous codebases (Pro, Terminal-Bench) see a smaller cost edge, and a handful of tasks (`tutanota`, `vuls`, `flipt`) cost LemonCrow *more* than baseline -- a fixed per-run overhead that amortizes on bigger tasks but not small/turn-heavy ones.
- **Bottom line:** the cost/token/turn compression reproduces across every suite tested, at every price point from a $0.10 task to a $5 one. It hasn't cost correctness on Verified or Pro; Lite is -2.0pp this cut; Terminal-Bench ties on correctness but is still 16.0% cheaper once cache-write pricing is normalized to a matched tier. Read each section's caveats before citing a number out of context.
