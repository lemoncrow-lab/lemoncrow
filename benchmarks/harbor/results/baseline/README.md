# Baseline: Claude Code 2.1.205 / Claude Opus 4.8 on Terminal-Bench 2.1

Reference numbers scraped from Harbor Hub, for comparing LemonCrow's own
Harbor runs (`../lemoncrow/<run>/...`) against an official Claude Code result
on the same benchmark.

**Source (view live here):**
https://hub.harborframework.com/datasets/terminal-bench/terminal-bench-2-1/6/leaderboards/main/rows/dcd48d03-9df9-46ab-bc4c-ade6dc35b8da?tab=results

Job `lb-pr-89/85493815-d3a7-4607-a14e-bb842d7931ba`, run 2026-07-09, agent
`claude-code` version `2.1.205`, model `claude-opus-4-8`, dataset
`terminal-bench/terminal-bench-2-1`. This supersedes an earlier baseline cut
scraped from the public tbench.ai leaderboard (Claude Code `2.1.152`) --
that data is no longer on disk here; see History below if you need it.

## How this was collected

Harbor Hub's row-detail page (`.../rows/<row-id>?tab=results`) is a
client-rendered app, but its results table is paginated at 100 trials/page
via a plain `?page=N` query param and returns full server-rendered content
-- no login or JS execution needed to read it, just `?page=1` through
`?page=5` for this row's 445 trials (verified complete: 89 distinct tasks x
5 reps each, 0 duplicates across pages). Each row has: task, agent, version,
model, reward, duration, timestamp, error type, input/output/cache tokens,
and cost -- a superset of what the old tbench.ai per-task pages exposed,
critically including **per-trial duration**, which tbench.ai's leaderboard
did expose but only in aggregate. Ad-hoc scraping script not checked in;
re-run by re-fetching the row URL above (paginate with `&page=N`) if the
data needs refreshing.

## Files

- **`tbench_opus48_claudecode_2.1.205_tasks.csv`** -- one row per trial (89
  tasks x 5 reps = 445, all present). Columns: `task, trial_name, result,
  reward, input_tokens, output_tokens, cache_tokens, total_tokens, cost_usd,
  duration, duration_seconds, timestamp, error_type, agent_version`.
  `input_tokens` is total input including cache (matches the old tbench.ai
  convention); `cache_tokens` is combined read+write (see Known gaps).

- **`tbench_opus48_claudecode_2.1.205_per_task.csv`** -- one row per task,
  rolled up across its 5 reps. Columns: `task, n_reps, n_pass, n_fail,
  n_no_data, pass_rate, avg_input_tokens, avg_output_tokens,
  avg_cache_tokens, avg_total_tokens, total_cost_usd, avg_cost_usd,
  total_duration_seconds, avg_duration_seconds`.

- **`summary.txt`** -- single-run-level rollup: totals and per-trial /
  per-task(x5) averages for tokens, cost, and duration; overall pass rate;
  costliest / longest-running / lowest-pass-rate tasks. Generated from the
  per-trial CSV above.

- **`tbench_opus48_claudecode_2.1.205_aggregate.csv`** -- grand totals, two
  rows for two different scopes (same columns: `scope, n_tasks,
  n_trials_or_tasks, n_pass, n_fail, n_no_data, pass_rate,
  total_input_tokens, total_output_tokens, total_cache_tokens,
  total_tokens, total_cost_usd, total_duration_seconds,
  total_duration_hours`):
  - `all_reps` -- sum over every one of the 445 trials (all 5 reps of all 89
    tasks). This is "run the whole suite 5x".
  - `one_run` -- sum of each task's *per-task average* (from
    `per_task.csv`), i.e. the cost of a single pass through all 89 tasks
    (one rep each). `n_pass`/`n_fail` here are expected values (sum of
    per-task pass rates), not integers, since the average blends reps that
    did and didn't pass.

## Turn / tool-call data (`tbench_opus48_claudecode_2.1.205_turns.csv`)

One row per trial: `trial_id, task, trial_name, n_turns, n_tool_calls,
prompt_tokens, completion_tokens, cache_read_tokens, cache_creation_tokens,
cost_usd`. `n_turns` = number of agent-authored steps in the trial's
trajectory (Harbor's ATIF format), directly comparable to LemonCrow's own
`num_turns` from `agent/claude-run.json`.

Contrary to earlier assumptions in this repo's history, baseline's full
per-trial trajectory *is* fetchable, just not from a documented endpoint:
Harbor Hub's trial-detail page is client-rendered, but the row-listing page
embeds each trial's real UUID + job_id in a Next.js RSC payload that a
naive HTML fetch (one that strips `<script>` tags) misses. With those IDs,
an undocumented same-origin API serves the full trajectory, no auth needed:

```
GET https://hub.harborframework.com/api/trials/{trial_id}/trajectory
    ?jobId={job_id}&trajectory_path=trials/{trial_id}/trajectory.json
```

Regenerate with `uv run python benchmarks/harbor/scrape_baseline_trajectories.py`
(script self-checks its scrape against the known real cost total before
writing output). Raw trajectories (~290MB for all 445, one alone 178MB) are
not checked in -- only this small rollup.

**Headline finding (2026-07-18):** matched against LemonCrow's own 428
priced trials from `2026-07-14__13-44-30`, average turns/trial are close
(baseline 15.8 vs LemonCrow 17.88, +13%), so turn *count* is not the main
cost driver. What differs sharply is turn *size*: baseline reads ~19,008
cache tokens per turn vs LemonCrow's ~26,192 (+38%) -- most turns carry
more context on LemonCrow's side. The trajectories also show baseline's
Claude Code batches multiple tool calls into a single turn 7.9% of the
time (up to 8 calls in one turn; baseline only ever calls `Bash`/`Read`/
`Edit`), while LemonCrow's 445 trials show **0%** multi-tool-call turns.
Baseline's `cache_creation.ephemeral_1h_input_tokens` is 0 on every step
sampled -- direct confirmation (not inference) that baseline runs on the
5-minute cache tier.

## Known gaps

- **Cache tokens are combined read+write.** Harbor Hub's results table
  exposes a single `cache_tokens` figure per trial -- no cache-write /
  cache-read split available from this source.
- **34 of 445 trials have no `cost_usd`.** All 34 are `AgentTimeoutError`
  (agent hit its wall-clock budget before Claude Code wrote a final cost
  message) -- tokens and duration are still recorded for these, only cost is
  blank. 32 of the 34 also have a recorded `reward` (some pass, most fail);
  reward is graded independently of whether cost was captured.
- **3 trials have no reward at all** (`result = no data`): duration/tokens
  may still be present. Distinct from the 91 trials that failed cleanly
  with `reward = 0.00`.
- **1 trial (`protein-assembly__f789d8f3`) hit `UnknownApiError`** after 43s
  with `reward = 0.00` and a tiny $0.01 charge -- a fast API-level failure,
  not a timeout.

## Cost comparison vs. LemonCrow's own run

Both sides use their own real, self-reported `cost_usd` -- no re-pricing.

**`lemoncrow_vs_baseline_per_task.csv`** -- per-task comparison against a
LemonCrow Harbor run. Columns: `task, baseline_resolved (x/5),
lemoncrow_resolved, baseline_avg_cost_raw, lemoncrow_cost, save_pct`.
`baseline_avg_cost_raw` is copied straight from
`tbench_opus48_claudecode_2.1.205_per_task.csv`'s `avg_cost_usd`;
`lemoncrow_cost` is the LemonCrow run's own `agent_result.cost_usd`.

**Status: this file needs a fresh run before citing it** -- it hasn't been
regenerated against a settled (non-live) LemonCrow Harbor run yet:

```bash
# bump RUN_DIR in compare_current_lemoncrow_to_baseline.py to a settled
# LemonCrow Harbor run dir, then:
uv run python benchmarks/harbor/compare_current_lemoncrow_to_baseline.py
uv run python scripts/gen_harbor_cost_vs_savings_scatter.py
```

## Regenerating the rollups

`summary.txt`/`per_task.csv`/`aggregate.csv` are computed purely from the
per-trial CSV -- if that CSV is updated/replaced, regenerate them from it
(group by `task`, average/sum the numeric columns, skip blank cells).
`lemoncrow_vs_baseline_per_task.csv` is regenerated by
`uv run python benchmarks/harbor/compare_current_lemoncrow_to_baseline.py`.

## History

The original baseline cut (Claude Code 2.1.152, scraped from the public
tbench.ai leaderboard per-task pages, 440/445 trials present, no per-trial
duration) was fully replaced by this 2.1.205/Harbor Hub cut on 2026-07-18 --
it added complete duration data and 5 more trials, at the cost of no longer
matching the specific Claude Code point-release cited in some older
BENCHMARKS.md/README.md prose (those need a follow-up refresh against this
new baseline; see the repo's top-level docs for current status).
