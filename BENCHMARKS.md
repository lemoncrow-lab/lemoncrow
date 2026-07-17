# LemonCrow Benchmarks

This document keeps benchmark proof out of the first-use README while preserving the evidence trail for the headline claims.

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
| Terminal-Bench 2.1, 89 tasks x 1 rep vs public leaderboard x 5 reps                                                                                                                                                                                                     |       70 / 89 resolved (78.7%) | **70.25 / 89 expected (78.9%)** |      -0.2 percentage points |
| Terminal-Bench cost, 83/89 tasks w/ cost data                                                                                                                                                                                                                           |            **$69.52** | $96.76 |             **28.1%\* cheaper** |                             |
| \* Understates LemonCrow's savings floor, not overstates it -- 5 of the 6 tasks missing cost data are real, uncounted LemonCrow spend (harness killed the process on a timeout before it could log a final cost), not zero-cost runs. See the Terminal-Bench section below. |                                |                                 |                             |

## SWE-bench Verified

End-to-end bug fixing on 50 SWE-bench Verified instances across 12 Python repos, with 5 reps each. Both arms used the same model, same Docker image, same conda environment, same turn cap, same timeout, and same disabled tools. The LemonCrow arm used `lemoncrow:auto`.


| Arm         |        Cost | Input tok | Cache write |  Cache read | Output tok |  Total tok |     Turns |      Time |       Resolved       |
| ------------- | ------------: | ----------: | ------------: | ------------: | -----------: | -----------: | ----------: | ----------: | :---------------------: |
| **LemonCrow** | **$165.45** | 1,007,977 |   5,730,565 |  97,238,294 |  2,192,112 | **106.2M** | **4,336** | **10.9h** | **232 / 250 (92.8%)** |
| Baseline    |     $234.84 | 1,118,221 |   7,036,456 | 181,596,567 |  3,039,396 |     192.8M |     6,962 |     14.3h |   202 / 250 (80.8%)   |
| Delta       |      -29.5% |     -9.9% |      -18.6% |      -46.5% |     -27.9% |     -44.9% |    -37.7% |    -23.7% |       +12.0 pp       |

Raw data: [`benchmarks/codebench/results/swe50_2026_06_30/`](benchmarks/codebench/results/swe50_2026_06_30/)

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

## SWE-bench Lite

A smaller companion cut: 10 SWE-bench Lite instances x 5 reps, same harness (`multiswe_run.py`), same model, same disabled-tools list, and the same `lemoncrow:auto` persona as the Verified run above.


| Arm         |       Cost | Input tok | Cache write |  Cache read | Output tok |  Total tok |   Turns |        Time |     Resolved     |
| ------------- | -----------: | ----------: | ------------: | ------------: | -----------: | -----------: | --------: | ------------: | :-----------------: |
| **LemonCrow** | **$17.51** |   150,236 |     601,817 | 11,582,911 |    197,782 | **12.53M** | **689** | **66.5min** |   48 / 50 (96%)   |
| Baseline    |     $19.83 |   198,203 |     669,766 | 12,180,657 |    251,465 |     13.30M |     771 |     68.8min | **49 / 50 (98%)** |
| Delta       |     -11.7% |    -24.2% |      -10.1% |       -4.9% |     -21.3% |      -5.8% |  -10.6% |       -3.2% |     -2.0 pp     |

Raw data: [`benchmarks/codebench/results/swe-lite_2026-07-16/`](benchmarks/codebench/results/swe-lite_2026-07-16/).

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

Raw data: [`benchmarks/codebench/results/swe-pro_2026_07_07/`](benchmarks/codebench/results/swe-pro_2026_07_07/) -- the original single-invocation 2026-07-06 rep1 (with the protonmail dead slot) is kept at [`benchmarks/codebench/results/swe-pro_2026-07-06/`](benchmarks/codebench/results/swe-pro_2026-07-06/) for history.

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

## Exploration Tasks

7 open-source codebases, 1 exploration question each, 5 reps per arm, `claude-opus-4-8`. Costs are summed across all reps. The baseline arm is the 2026-06-29 run; the LemonCrow arm was re-run on 2026-07-08 against the current runtime — same tasks, prompts, model, timeout, and driver (protocol recorded in the run's `benchmark-manifest.json`).


| Codebase   | Language / size                   |                LemonCrow |        Baseline | Cost delta |
| ------------ | ----------------------------------- | -----------------------: | ----------------: | -----------: |
| Tokio      | Rust, 784 files, 176k lines       |          $0.34 | $2.69 | **87% cheaper** |            |
| Alamofire  | Swift, 98 files, 44k lines        |          $0.74 | $4.83 | **85% cheaper** |            |
| Django     | Python, 3k files, 522k lines      |          $0.37 | $2.31 | **84% cheaper** |            |
| OkHttp     | Java, 596 files, 133k lines       |          $0.29 | $1.60 | **82% cheaper** |            |
| VS Code    | TypeScript, 11k files, 3.3M lines |          $0.72 | $3.08 | **77% cheaper** |            |
| Gin        | Go, 99 files, 24k lines           |          $0.29 | $1.09 | **73% cheaper** |            |
| Excalidraw | TypeScript, 600 files, 171k lines |          $3.54 | $3.51 |    +0.7% (even) |            |
| **Total**  | 7 repos, 16k files, 4.4M lines    | **$6.29** | **$19.11** | **67% cheaper** |            |

Honest outlier: Excalidraw is a dead heat ($3.54 vs $3.51) — the one repo where LemonCrow's answer style spends as much as it saves. Every other repo is 73–87% cheaper. Beyond cost: 91% fewer turns (1,237 → 112), 92% fewer cache-read tokens, 84% fewer output tokens, at equal wall-clock.

Raw data: [`benchmarks/codebench/results/exploration_2026_06_29/`](benchmarks/codebench/results/exploration_2026_06_29/)

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

Raw data: [`benchmarks/codebench/results/telegraphic_2026_07_17/`](benchmarks/codebench/results/telegraphic_2026_07_17/) -- includes `summary.csv`, the full `results.jsonl` (300 rows: baseline/lemoncrow/caveman x 20 prompts x 5 reps), and per-call `.flow_dump.txt` transcripts (raw `.flow` wire captures are gitignored; they carry bearer tokens).

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

Pure retrieval quality was measured against common CLI and MCP code-search tools on the same 14 repos and roughly 7.2k query/gold pairs. LemonCrow reports three internal channels: lexical default, optional `+zoekt`, and optional `+semantic`. Every provider is scored across all 5 gold kinds (definition, content, semantic, swebench, sessions) -- a provider with no content/text-search capability (codegraph, universal-ctags) scores 0 on the kinds it cannot answer rather than being excluded from them, so `n` is uniform (7213) across every row in the table and MRR is directly comparable throughout.


| Provider                    |       MRR |     rec@1 |     rec@2 |     rec@3 |     p95 |     p100 |    n |
| ----------------------------- | ----------: | ----------: | ----------: | ----------: | --------: | ---------: | -----: |
| ⭐ LemonCrow lexical, default |     0.676 |     0.582 |     0.700 |     0.743 |   134ms |    319ms | 7213 |
| LemonCrow +zoekt              |     0.676 |     0.582 |     0.700 |     0.743 |   125ms |    359ms | 7213 |
| **LemonCrow +semantic (BGE)** | **0.727** | **0.650** | **0.757** | **0.783** |   390ms |   1057ms | 7213 |
| cocoindex-code              |     0.557 |     0.457 |     0.567 |     0.625 |   595ms |   2061ms | 7213 |
| codebase-memory-mcp         |     0.502 |     0.437 |     0.511 |     0.553 |   541ms |   1817ms | 7213 |
| fff-mcp                     |     0.430 |     0.388 |     0.434 |     0.456 |    46ms |    207ms | 7213 |
| serena                      |     0.401 |     0.359 |     0.405 |     0.424 |  3834ms | 269001ms | 7213 |
| ripgrep                     |     0.376 |     0.320 |     0.376 |     0.405 |    66ms |    522ms | 7213 |
| code-index-mcp              |     0.343 |     0.296 |     0.345 |     0.371 |   377ms |   3830ms | 7213 |
| ast-grep                    |     0.312 |     0.271 |     0.317 |     0.341 |  1255ms |   8806ms | 7213 |
| jcodemunch-mcp              |     0.299 |     0.226 |     0.289 |     0.341 |   214ms |   4189ms | 7213 |
| codegraph                   |     0.296 |     0.267 |     0.299 |     0.316 |    17ms |    532ms | 7213 |
| universal-ctags             |     0.237 |     0.226 |     0.242 |     0.245 | **1ms** | **12ms** | 7213 |

Both `LemonCrow lexical` and `+semantic` rows are 2026-07-06 re-runs after a latency fix (an unbounded ANN-matrix cache-miss path) and a harness measurement bug (the bench server was paying its own statusline pipeline inside timed queries); other rows' latencies predate that fix and may be pessimistic.

Raw data and per-repo details: [`benchmarks/codebench/results/retrieval_2026_07_05/`](benchmarks/codebench/results/retrieval_2026_07_05/)

Run it:

```bash
uv run lemoncrow eval retrieval --channel all --full --resume --csv /tmp/retrieval_mrr.csv

# quick smoke test
lc eval retrieval
```

## Indexing Time

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

Agentic terminal tasks on Terminal-Bench 2.1 through the Harbor harness, one attempt per task, `claude-opus-4-8`. The baseline isn't a matched run of ours -- it's the public tbench.ai leaderboard entry for Claude Code 2.1.152 / Opus 4.8 (5 reps/task), scraped per-task and **re-priced to LemonCrow's real cache pricing** (tbench.ai bills cache tokens at $0; LemonCrow pays cache-read $0.50/M and 1h-ephemeral cache-write $10/M -- full re-pricing methodology in `benchmarks/harbor/results/baseline/README.md`). With that correction applied, cost is now a genuine per-task comparison; correctness compares LemonCrow's single attempt against the leaderboard's 5-rep average pass rate.


| Arm         |       Cost | Input tok (fresh) |  Cache tok | Output tok | Total tok |
| ------------- | -----------: | ------------------: | -----------: | -----------: | ----------: |
| **LemonCrow** | **$69.52** |           256,219 | 38,734,280 |  1,332,650 | **40.3M** |
| Baseline    |     $96.76 |         2,815,972 | 45,234,128 |  1,490,257 |     49.5M |
| Delta       |     -28.1% |            -90.9% |     -14.4% |     -10.6% |    -18.6% |

83 of 89 tasks have cost data on both sides; the other 6 hit `AgentTimeoutError` before Claude Code wrote a final `cost_usd`, so their real (non-zero) LemonCrow spend goes uncounted -- **$69.52 understates LemonCrow's true total**, and the real gap to baseline's $96.76 is narrower than 28.1%, possibly less (full breakdown: `benchmarks/harbor/results/baseline/README.md`). Resolved over all 89 tasks regardless of cost data: LemonCrow **70/89 (78.7%)** vs baseline's 5-rep average of **70.25/89 (78.9%)** -- a tie at n=1.


| Baseline task cost    | n tasks               | avg baseline  | avg lemoncrow   | avg delta |
| ----------------------- | ----------------------- | --------------- | --------------- | ----------- |
| < $0.50 | 34 | $0.29  | $0.34 | +$0.05 (1.2x) |               |               |           |
| $0.50-$1.50           | 31                    | $0.86 | $0.68 | -$0.17 (0.8x) |           |
| >= $1.50 | 18 | $3.35 | $2.04 | -$1.31 (0.6x) |               |               |           |


![Terminal-Bench cost vs savings, LemonCrow vs re-priced public leaderboard baseline](benchmarks/harbor/results/baseline/cost_vs_savings_scatter.png)

Crisp/zoomable version: [`benchmarks/harbor/results/baseline/cost_vs_savings_scatter.svg`](benchmarks/harbor/results/baseline/cost_vs_savings_scatter.svg). Regenerate with `uv run python scripts/gen_harbor_cost_vs_savings_scatter.py` after refreshing the per-task CSVs (see command below).

Raw data: [`benchmarks/harbor/results/lemoncrow/2026-07-07__02-24-29/`](benchmarks/harbor/results/lemoncrow/2026-07-07__02-24-29/); re-priced baseline + full methodology: [`benchmarks/harbor/results/baseline/`](benchmarks/harbor/results/baseline/).

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

Refresh the baseline comparison after a new run (bump `RUN_DIR` in `compare_current_lemoncrow_to_baseline.py` first):

```bash
uv run python benchmarks/harbor/compare_current_lemoncrow_to_baseline.py
uv run python benchmarks/harbor/normalize_baseline_cost.py
uv run python scripts/gen_harbor_cost_vs_savings_scatter.py
```

## Overall Assessment

- **Cost/tokens/turns: LemonCrow wins on every suite measured.** Verified -29.5% cost/-44.9% tokens/-37.7% turns, Lite -11.7%/-5.8%/-10.6%, Pro -21.5%/-35.4%/-28.1%, Exploration -67% cost, Terminal-Bench -28.1% cost (a floor -- 5 timed-out LemonCrow trials have real, uncounted spend), Telegraphic Q&A -46.7% cost/-60.9% output tokens (every token category fell this run, cost and tokens agree). Caveman (free DIY "be terse" system-prompt instruction, no plugin/tooling) cuts output by 41.4% but now costs **3.3% more** than baseline -- it only compresses replies, not the input/context tokens that drive the bill.
- **Correctness wins on most multi-rep suites.** Verified +12.0pp, Pro +2.0pp (the 5-rep run overturned an earlier single-rep -10.0pp result -- that loss was n=1 noise), Lite -2.0pp. Terminal-Bench is a tie (-0.2pp) at n=1, unresolved either way.
- **Where overhead still shows up:** non-Python/larger/more heterogeneous codebases (Pro, Terminal-Bench) see a smaller cost edge, and a handful of tasks (`tutanota`, `vuls`, `flipt`, ~a third of Terminal-Bench's sub-$0.50 tasks) cost LemonCrow *more* than baseline -- a fixed per-run overhead that amortizes on bigger tasks but not small/turn-heavy ones.
- **Bottom line:** the cost/token/turn compression reproduces across every suite tested, at every price point from a $0.10 task to a $5 one. It hasn't cost correctness on Verified or Pro; Lite is -2.0pp this cut, and Terminal-Bench (n=1) remains unresolved either way. Read each section's caveats before citing a number out of context.
