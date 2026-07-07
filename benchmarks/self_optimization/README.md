# self_optimization

Benchmark/test tooling, not product runtime. An example fitness function for
the swarm system's generic `best` reducer — it lets a swarm run optimize
Atelier *against itself*, holding correctness fixed while it searches for
cheaper/cleaner behavior.

## Pipeline

1. **`freeze_baseline.py`** — snapshot vanilla-Claude-Code cost/solve-rate per
   task from a prior graded `results.jsonl` run. The baseline arm is the same
   model as the atelier arm and invariant to source edits, so it's measured
   once and frozen (`baseline/swe30.json`), not re-run every iteration.
2. **`make_holdout.py`** — samples a held-out task split, disjoint from the
   frozen target set (`tasks/holdout.txt`), so a win can be checked against
   tasks the loop never iterated against (guards overfitting).
3. **`eval.py`** — the *immutable* objective function a candidate is scored
   with. Do not change its metric definitions casually; comparability across
   experiments depends on them staying fixed.

## Objectives (`eval.py --objective ...`)

- **`health`** (default, free): tests must pass (hard gate), then
  `score = -(mypy_errors + ruff_issues)` — no API spend.
- **`mini`** (paid): cost-per-accepted-patch from `atelier benchmark mini --json`.
- **`swe`** (paid): runs the atelier arm on `tasks/iterate.txt` or
  `tasks/holdout.txt`, compares $ cost and solve-rate against the frozen
  baseline. Target: ≥50% cheaper with correctness same-or-better.

`knobs.env` holds the env-var overrides `eval.py --objective swe` injects into
the atelier arm for one run (a controlled, single-variable experiment).

## Wiring it into a swarm run

```bash
uv run atelier swarm start \
    --fitness-cmd "uv run python benchmarks/self_optimization/eval.py --objective health" \
    --metric-parse "regex:^score: (-?[\d.]+)"
```

`eval.py` prints a `key: value` block between `---` fences, not bare JSON or a
trailing number — pair `--fitness-cmd` with an explicit `--metric-parse`
targeting the `score:` line (swarm's default `stdout_float` parser grabs the
*last* numeric token in stdout, which is `eval_seconds`, not `score`).

## Manual run

```bash
uv run python benchmarks/self_optimization/eval.py --objective swe \
    --tasks benchmarks/self_optimization/tasks/iterate.txt --reps 1 \
    --json benchmarks/self_optimization/last.json \
    --log benchmarks/self_optimization/results.tsv --desc "tighten types in store"
```
