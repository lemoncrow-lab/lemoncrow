# Baseline: Claude Code 2.1.152 / Claude Opus 4.8 on Terminal-Bench 2.1

Reference numbers scraped from the public tbench.ai leaderboard, for comparing
Atelier's own Harbor runs (`../atelier/<run>/...`) against the official
Claude Code result on the same benchmark.

**Source (view live here):**
https://www.tbench.ai/leaderboard/terminal-bench/2.1/claude-code/2.1.152/claude-opus-4-8%40anthropic

Each task on that page links to a per-task detail view
(`.../claude-opus-4-8%40anthropic/<task-checksum>`) listing all 5 trials with
input/output/cache tokens, cost, and duration — that per-trial table is what
was scraped here.

## How this was collected

tbench.ai is a Next.js app that ships each page's data as a JSON blob
embedded in the server-rendered HTML (no public API). The data was pulled
straight from that HTML with `curl` — fetch the model's leaderboard page to
get the task → checksum map, then fetch each task's detail page and parse its
rendered results table. No browser/JS execution needed. Ad-hoc scripts used
for this are not checked in; re-run by re-scraping the URL above if the data
needs refreshing (e.g. after a new terminal-bench version or rep).

## Files

- **`tbench_opus48_claudecode_2.1.152_tasks.csv`** — one row per trial
  (89 tasks × 5 reps = 445 expected, 440 present; see Gaps below).
  Columns: `task, trial_name, result, input_tokens, output_tokens,
  cache_tokens, total_tokens, cost_usd, duration, duration_seconds`.

- **`tbench_opus48_claudecode_2.1.152_per_task.csv`** — one row per task,
  rolled up across its reps. Columns: `task, n_reps, n_pass, n_fail,
  n_no_data, pass_rate, avg_input_tokens, avg_output_tokens,
  avg_cache_tokens, avg_total_tokens, total_cost_usd, avg_cost_usd,
  total_duration_seconds, avg_duration_seconds`.

- **`summary.txt`** — single-run-level rollup: totals and per-trial /
  per-task(×5) averages for tokens and cost, total wall-clock duration,
  overall pass rate, and the costliest / lowest-pass-rate tasks. Generated
  from the per-trial CSV above.

- **`tbench_opus48_claudecode_2.1.152_aggregate.csv`** — grand totals, two
  rows for two different scopes (same columns: `scope, n_tasks,
  n_trials_or_tasks, n_pass, n_fail, n_no_data, pass_rate,
  total_input_tokens, total_output_tokens, total_cache_tokens,
  total_tokens, total_cost_usd, total_duration_seconds,
  total_duration_hours`):
  - `all_reps` — sum over every one of the 440 trials (all 5 reps of all 89
    tasks). This is "run the whole suite 5x": $288.15, 73.4h.
  - `one_run` — sum of each task's *per-task average* (from
    `per_task.csv`), i.e. the cost of a single pass through all 89 tasks
    (one rep each): $57.90, 14.8h. `n_pass`/`n_fail` here are expected
    values (sum of per-task pass rates), not integers, since the average
    blends reps that did and didn't pass.

## Known gaps (verified against tbench.ai itself, not scraping bugs)

- **Cache tokens are combined read+write.** tbench.ai only exposes a single
  `cache_tokens` figure per trial, in both its rendered table and its
  underlying data — there is no cache-write / cache-read split available
  from this source at any granularity.
- **`rstan-to-pystan`** has only 4 of 5 reps on tbench.ai — the 5th isn't in
  their table at all.
- **`compile-compcert__gFqkD3K`** ran (60m 36s, recorded as a fail) but
  tbench.ai shows `N/A` for its tokens/cost, left blank here to match.
- **`protein-assembly`** has zero per-trial data on tbench.ai (page shows
  "No trial data available"); all 5 reps failed with no telemetry captured.
  Represented as a single placeholder row with `result = no data available`.

## Cost comparison vs. Atelier's own run (`../atelier/2026-07-07__02-24-29/`)

**`atelier_vs_baseline_per_task.csv`** — per-task comparison against Atelier's
Harbor run, matched on the 83 tasks both sides have cost data for. Columns:
`task, baseline_resolved (x/5), atelier_resolved, baseline_avg_cost_corrected,
atelier_cost, save_pct, baseline_rep_costs_corrected` (JSON list of the 5
corrected per-rep costs).

**Why "corrected":** tbench.ai's displayed cost treats cache tokens as **$0**
(see Known gaps below) while Atelier's runs pay the real bill — cache reads
($0.50/M) and **1h ephemeral cache writes ($10/M, 2x input)**. To compare like
for like, baseline costs here are recomputed as
`(input − cache) × $5/M + output × $25/M + cache × blended_cache_rate`,
where the blended cache rate prices a **5.13% write share at the 1h write
rate** and the remainder as reads (≈ **$0.99/M**). The write share is the
token-weighted `cache_creation_input_tokens` vs `cache_read_input_tokens`
ratio measured across all 374 Atelier Harbor trials' `agent/claude-run.json`
usage reports on disk (every rep of every run under `../atelier/`, not just
the current one). tbench.ai doesn't expose a per-trial read/write split, so
this ratio is an estimate applied uniformly, not a measured value per
baseline trial. Regenerate both derived CSVs -- bump `RUN_DIR` in
`compare_current_atelier_to_baseline.py` to the new run first -- with
`uv run python benchmarks/harbor/compare_current_atelier_to_baseline.py &&
uv run python benchmarks/harbor/normalize_baseline_cost.py` (the first
refreshes `atelier_resolved`/`atelier_cost` from `RUN_DIR`; the second
re-blends the baseline columns against them).

**Result:** on the 83/89 tasks where both arms actually have cost telemetry,
Atelier totals **$75.87** vs. baseline's corrected **$96.02** (**0.79x —
21.0% cheaper**). Same direction as the prior cut (0.84x, 16.4% cheaper) --
this refresh adds cost data for 5 tasks that had previously timed out, and
the wider sample lands slightly cheaper still. Caveat below: this $75.87 is
an undercount -- 5 of the 6 remaining missing tasks are real Atelier spend
with no recorded number, not tasks that were free or excluded by choice.

| Baseline task cost | n tasks | avg baseline | avg atelier | avg delta |
|---|---|---|---|---|
| < $0.50 | 34 | $0.29 | $0.34 | +$0.05 (1.2x) |
| $0.50–$1.50 | 31 | $0.85 | $0.68 | -$0.17 (0.8x) |
| ≥ $1.50 | 18 | $3.32 | $2.39 | -$0.93 (0.7x) |

Cheap tasks still run a mild ~20% overshoot on Atelier; mid and large tasks
both come out cheaper. 27/83 tasks cost more on Atelier, 56/83 cost less.
Picture: [`cost_vs_savings_scatter.svg`](cost_vs_savings_scatter.svg)
(regenerate with `uv run python scripts/gen_harbor_cost_vs_savings_scatter.py`
after refreshing the CSVs above).

**Why 6 tasks are missing cost data, and why that's not the same as
"excluded by choice":** 5 of them --
`extract-moves-from-video`, `gpt2-codegolf`, `mailman`, `make-doom-for-mips`,
`rstan-to-pystan` -- all hit the harness's AgentTimeoutError (exactly the
`n_errors: 5` in the run's own `result.json`). Claude Code only writes
`cost_usd` in its final `result` stream message once the whole turn loop
finishes; the harness kills the process the moment it exceeds the task's
wall-clock budget, so that final message never gets written. The tokens were
real and Anthropic billed for them -- the number is just never captured by
the harness. Reward is unaffected (it's graded from on-disk task state, not
the killed process), so 1 of these 5 (`mailman`) actually **passed** despite
having no cost. The 6th, `protein-assembly`, is the one genuine
like-for-like gap: Atelier finished it normally (cost **$0.0311**, failed),
but tbench.ai itself has zero per-trial telemetry for this task on the
baseline side (a pre-existing gap, see Known gaps below) so there's nothing
to compare it against.

Net effect: **$75.87 understates Atelier's true total** -- add whatever the
5 timed-out trials actually cost (each ran up to its full timeout budget, so
not small) and the real gap to baseline's $96.02 is narrower than 21.0%,
possibly reversed. Correctness across all 89 tasks (cost data or not, since
reward doesn't depend on the cost report): Atelier resolved **69/89
(77.5%)**; baseline's tbench.ai 5-rep average implies **70.25/89 expected
(78.9%)**. Atelier trails by 1.4 points on correctness while running
nominally 21.0% cheaper on the 83 tasks with telemetry -- read together with
the SWE-bench Pro result in the top-level `BENCHMARKS.md`, correctness is the
axis to watch here, not cost.

## Regenerating the rollups

`summary.txt`/`per_task.csv`/`aggregate.csv` are computed purely from the
per-trial CSV — if that CSV is updated/replaced, regenerate them from it
(group by `task`, average/sum the numeric columns, skip blank cells).
`normalized_cost.csv` and `atelier_vs_baseline_per_task.csv` are regenerated
by `uv run python benchmarks/harbor/normalize_baseline_cost.py` (see
`normalized_cost.README.txt` for the cost model).
