# Benchmark Results

Every number on [atelier.ws](https://atelier.ws) and in the repo's
[BENCHMARKS.md](https://github.com/atelier-ws/atelier/blob/main/BENCHMARKS.md)
traces back to a committed raw run. This page is the index: what was measured,
against what, and where the receipts live. See also: [every "vs" comparison on
atelier.ws](https://atelier.ws/vs) for the marketing-facing version of the same
data, and [Marketing & Positioning](../marketing/strategy.md) for voice.

## Suite index

| Suite | What it measures | Baseline | Atelier | Raw data |
| --- | --- | ---: | ---: | --- |
| SWE-bench Verified | 50 tasks x 5 reps, real bug fixes, official harness | 202/250 (80.8%), $234.84 | **232/250 (92.8%), $165.45** | [`swe50_2026_06_30/`](https://github.com/atelier-ws/atelier/tree/main/benchmarks/codebench/results/swe50_2026_06_30) |
| SWE-bench Lite | 10 tasks x 3 reps | 28/30 (93.3%), $12.38 | **30/30 (100%), $10.79** | [`swe-lite_2026-07-06/`](https://github.com/atelier-ws/atelier/tree/main/benchmarks/codebench/results/swe-lite_2026-07-06) |
| SWE-bench Pro | 10 tasks x 5 reps, Go/TS/Python, ScaleAI harness | 44/50 (88%), $39.01 | **45/50 (90%), $30.61** | [`swe-pro_2026_07_07/`](https://github.com/atelier-ws/atelier/tree/main/benchmarks/codebench/results/swe-pro_2026_07_07) |
| Exploration tasks | 7 large repos x 5 reps, one open-ended question each | $19.11 | **$6.29 (67% cheaper)** | [`exploration_2026_06_29/`](https://github.com/atelier-ws/atelier/tree/main/benchmarks/codebench/results/exploration_2026_06_29) |
| Telegraphic output anatomy | Output-token decomposition (prose vs. fixed payload vs. thinking) | 67 prose tok/turn | **30 prose tok/turn (2.7x less)** | [`swe_lite_telegraphic_2026_07_06/`](https://github.com/atelier-ws/atelier/tree/main/benchmarks/codebench/results/swe_lite_telegraphic_2026_07_06) |
| Telegraphic Q&A | 20 engineering Q&A prompts x 5 reps, no repo, no golden patch | $8.93 | **$6.81 (23.7% cheaper)** | [`telegraphic_2026_07_08_5rep/`](https://github.com/atelier-ws/atelier/tree/main/benchmarks/codebench/results/telegraphic_2026_07_08_5rep) |
| Retrieval evaluation | Code-search quality (MRR/recall/latency) vs. 10 named tools, 14 repos, ~7,213 query/gold pairs | best rival 0.557 MRR (cocoindex-code) | **0.727 MRR (+semantic)** | [`retrieval_2026_07_05/`](https://github.com/atelier-ws/atelier/tree/main/benchmarks/codebench/results/retrieval_2026_07_05) |
| Indexing time | Cold full-rebuild time, 14 repos, lexical/zoekt/semantic phases | -- | see table below | same as above |
| Embedder sweep | 9 embedding models scored on definition/content/semantic MRR | best alternative 0.783 avg | **0.847 avg (BGE-Code-v1, Atelier's default)** | `benchmarks/codebench/run_embedder_sweep.py` |
| Terminal-Bench 2.1 | 89 agentic terminal tasks, 1 attempt vs. public tbench.ai leaderboard (5-rep avg) | 78.9% expected, $96.76 | 78.7%, **$69.52 (28.1% cheaper)*** | [`harbor/results/atelier/2026-07-07__02-24-29/`](https://github.com/atelier-ws/atelier/tree/main/benchmarks/harbor/results/atelier/2026-07-07__02-24-29) |

<sub>* Understates the real gap: 5 of 6 tasks missing cost data are real, uncounted Atelier spend (harness killed the process on a timeout before it logged final cost), not zero-cost runs.</sub>

Full per-suite tables, setup notes, and reproduction commands are in
[BENCHMARKS.md](https://github.com/atelier-ws/atelier/blob/main/BENCHMARKS.md) --
this page is the index, that file is the source of truth.

## Retrieval evaluation: the "vs named competitors" story

This is the one suite that isn't Atelier-vs-itself -- it's Atelier vs. 10
named, real code-search tools other people ship and use: ripgrep, ast-grep,
universal-ctags, Serena, CodeGraph, cocoindex-code, codebase-memory-mcp,
fff-mcp, code-index-mcp, and jCodeMunch. Every tool ran the identical 14-repo,
~7,213 query/gold-pair corpus with the identical scoring.

| Provider | MRR | rec@1 | p95 | p100 |
| --- | ---: | ---: | ---: | ---: |
| Atelier +semantic (BGE) | **0.727** | **0.650** | 390ms | 1057ms |
| Atelier lexical (default) | 0.676 | 0.582 | 134ms | 319ms |
| cocoindex-code | 0.557 | 0.457 | 595ms | 2061ms |
| codebase-memory-mcp | 0.502 | 0.437 | 541ms | 1817ms |
| fff-mcp | 0.430 | 0.388 | 46ms | 207ms |
| serena | 0.401 | 0.359 | 3834ms | 269001ms |
| ripgrep | 0.376 | 0.320 | 66ms | 522ms |
| code-index-mcp | 0.343 | 0.296 | 377ms | 3830ms |
| ast-grep | 0.312 | 0.271 | 1255ms | 8806ms |
| jcodemunch-mcp | 0.299 | 0.226 | 214ms | 4189ms |
| codegraph | 0.296 | 0.267 | 17ms | 532ms |
| universal-ctags | 0.237 | 0.226 | 1ms | 12ms |

What's checked, per tool (verbatim quotes, primary sources, verified before
publishing -- full detail per tool at [atelier.ws/vs](https://atelier.ws/vs)):

- **Publishes a real benchmark of some kind:** ripgrep (25-scenario speed
  comparison vs. grep/ag/ucg/pt/ack), jCodeMunch (95% token-reduction table
  with a stated methodology file), codebase-memory-mcp (a peer-reviewed
  arXiv paper, 83% answer quality vs. file-by-file exploration), cocoindex-code
  (70% token saving), fff-mcp (sub-10ms vs. ripgrep's 3-9s spawn on a 500k-file
  checkout), CodeGraph (WITH-vs-WITHOUT deltas on 7 repos), Serena (a
  third-party task-cost study, ManoMano's AEGIS benchmark).
- **Publishes nothing quantifiable:** code-index-mcp, ast-grep (a
  feature-comparison page only, no numbers), universal-ctags.
- **Has ever benchmarked itself against another code-search tool on
  retrieval accuracy (MRR/recall), the same axis this table measures:**
  none of the 10. ripgrep's and jCodeMunch's numbers above are the closest
  anyone gets, and both measure something else (raw text-search speed;
  token count against reading whole files) -- not whether the retrieved code
  was the right code.

That's the honest version of "nobody publishes real numbers here": several
tools publish real, checkable numbers about themselves, on their own terms,
against their own baseline. None had been scored against each other, on the
same corpus, before this table existed.

## Indexing time (cold full rebuild)

| Repo | Symbols | Lexical | Zoekt | Semantic (BGE-Code-v1) |
| --- | ---: | ---: | ---: | ---: |
| requests | 1,133 | 2.22s | 0.11s | 1.62s |
| django | 38,931 | 21.91s | 1.31s | 45.14s |
| linux | 1,239,077 | 179.49s | 13.69s | 1,208.89s |

Full 14-repo table in
[BENCHMARKS.md#indexing-time](https://github.com/atelier-ws/atelier/blob/main/BENCHMARKS.md#indexing-time).

## Vanilla Claude Code baseline

SWE-bench Verified/Lite/Pro, Exploration, Telegraphic Q&A, and Terminal-Bench
all compare Atelier against a clean Claude Code baseline -- same model, same
Docker image, same turn cap, same disabled-tools list, only the runtime
changes. Anthropic publishes Claude **model** accuracy on SWE-bench (e.g.
Opus 4.8: 88.6%) but not Claude Code's own cost/token/turn efficiency against
any baseline; GitHub is the one vendor we found publishing a comparable
harness-vs-harness efficiency study (GitHub Copilot's agent vs. model-vendor
harnesses). See [atelier.ws/vs/claude-code](https://atelier.ws/vs/claude-code)
for the marketing rollup, or
[BENCHMARKS.md](https://github.com/atelier-ws/atelier/blob/main/BENCHMARKS.md)
for every suite in full.

## Reproduce any of this

```bash
# SWE-bench Verified
CODEBENCH_ATELIER_AGENT=atelier:auto uv run --project benchmarks python -m benchmarks.codebench.multiswe_run \
  --suite swe-bench-verified --instances $(cat benchmarks/codebench/data/verified.txt) \
  --min-changed-files 1 -a baseline atelier --reps 5 --model claude-opus-4-8 --jobs 8

# Retrieval evaluation
uv run atelier eval retrieval --channel all --full --resume --csv /tmp/retrieval_mrr.csv

# Telegraphic Q&A
uv run atelier benchmark telegraphic --arm baseline --arm atelier --model claude-opus-4-8 --reps 5 --max-turns 50 --jobs 4 -y

# Terminal-Bench 2.1
atelier benchmark harbor -y
```

Every command above, plus setup notes and per-suite caveats, is in
[BENCHMARKS.md](https://github.com/atelier-ws/atelier/blob/main/BENCHMARKS.md).
