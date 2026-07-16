# LemonCrow Mini Eval

> This is an experimental development evaluation. Automatic provider/model
> routing is not part of the current product or plans.

The **mini eval** is the cheapest credible benchmark in LemonCrow: a small,
deterministic, cost-quality suite of 5–10 real tasks against this repository.
It exists to answer one question fast and honestly — *did routing produce
accepted patches cheaply, with trace evidence, and without regressions?* — with
no LLM judge and no way for token "savings" to hide a failure.

## Why it exists

Large benchmarks (TerminalBench, SWE-bench) are expensive and slow. The mini
eval gives a quick signal you can run locally:

- **Deterministic checks only.** Each case passes when its shell verify command
  exits `0`. No model grades another model.
- **Failures count.** Failed attempts still count toward `total_cost_usd` and
  the regression rate — they cannot disappear into "savings".
- **Success means accepted patches**, never token reduction alone.

## How to run

### Dry-run (offline, no API keys)

```bash
lc benchmark mini --dry-run --json
```

Dry-run loads and validates every case, then returns a report with
`status: "dry_run"` and every case `skipped`. It makes **no** git mutations,
**no** subprocess calls, and **no** API calls. Use it in CI to verify the suite
is well-formed without spending anything.

### Live run

```bash
lc benchmark mini --limit 5 --json
```

A live run requires a configured API key (e.g. `ANTHROPIC_API_KEY`). For each
case the runner:

1. Records the current git `HEAD`.
2. Stashes any uncommitted changes.
3. Resets to `starting_git_sha` (skipped when it is `HEAD`).
4. Drives the agent through the case prompt via `InteractiveRuntime`.
5. Records the trace id, tokens, and estimated cost.
6. Runs `command_to_verify` (pass = exit `0`).
7. Checks the patch and the `allowed_files` boundary.
8. Restores git state (reset + stash pop).

### Options

| Flag         | Default                          | Meaning                                  |
| ------------ | -------------------------------- | ---------------------------------------- |
| `--dry-run`  | off                              | Validate only; no API/git/subprocess.    |
| `--limit N`  | `5`                              | Max cases to run.                        |
| `--json`     | off                              | Print the JSON report to stdout.         |
| `--output`   | `.lemoncrow/evals/mini-report.json`| Where to write the JSON report.          |
| `--cases`    | `benchmarks/mini/cases.yaml`     | Path to the cases YAML.                  |

Both a JSON and a Markdown report are written next to each other (e.g.
`mini-report.json` and `mini-report.md`).

## Case format

Cases live in `benchmarks/mini/cases.yaml` under a top-level `cases:` list. Each
case validates against `MiniEvalCase`:

```yaml
cases:
  - id: mini-001-eval-mini-schema-doc
    title: "Add missing docstring to MiniEvalCase"
    prompt: |
      Add a module-level docstring to schema.py. One sentence is enough.
    starting_git_sha: HEAD            # "HEAD" means do not reset
    allowed_files:                    # globs the agent may change; [] = none
      - "src/lemoncrow/core/capabilities/eval_mini/schema.py"
    command_to_verify: "uv run python -c \"...\""   # passes on exit 0
    expected_success_condition: "File has a module docstring"
    max_cost_usd: 0.02                # soft ceiling
    tags: [docs, cheap]
```

If `allowed_files` is empty, the case fails if **any** file changes. If a
changed file matches none of the globs, the `file_boundary_respected` flag is
`false` and the case is `failed`.

## Report fields

The report validates against `MiniEvalReport`:

| Field                     | Meaning                                                                       |
| ------------------------- | ----------------------------------------------------------------------------- |
| `status`                  | `pass` / `fail` / `dry_run`. Derived from accepted patches, never from tokens.|
| `total_tasks`             | Cases run (after `--limit`).                                                  |
| `accepted_tasks`          | Cases where verify passed **and** the file boundary held.                     |
| `failed_tasks`            | Cases with status `failed` or `error`.                                        |
| `accepted_patch_rate`     | `accepted_tasks / total_tasks`.                                               |
| `total_cost_usd`          | Sum of estimated cost across **all** cases, including failures.               |
| `cost_per_accepted_patch` | `total_cost_usd / accepted_tasks` (falls back to total cost when none).        |
| `cheap_success_rate`      | Simplified as `accepted_tasks / total_tasks`.                                 |
| `routing_regression_rate` | `regression_cases / total_tasks` (always `0` for dry-run).                    |
| `context_reduction_pct`   | Optional context-reduction percentage (not measured by default).             |
| `trace_coverage_pct`      | Percent of non-skipped cases that carry a `trace_id`.                         |
| `cases`                   | Per-case `MiniEvalCaseResult` records.                                        |

A case is marked a **regression** when the agent created a patch but the result
was not accepted (verify failed or the file boundary was violated).

## Adding a new case

1. Append a `MiniEvalCase` entry to `benchmarks/mini/cases.yaml`.
2. Keep `command_to_verify` deterministic and cheap — it should exit `0` only
   when the task genuinely succeeded. Prefer `uv run …` for Python checks.
3. Scope `allowed_files` tightly so an over-broad edit is caught as a boundary
   violation.
4. Validate without spending anything:

   ```bash
   lc benchmark mini --dry-run --json
   ```

## Relationship to `make proof-cost-quality`

The mini eval shares the same philosophy as the WP-32 cost-quality proof gate
(`make proof-cost-quality`, `lc proof run`): accepted-patch economics,
trace coverage, and regression accounting where failures always count. The
proof gate is the formal release gate with fixed thresholds; the mini eval is
the fast, local, runnable companion you reach for during development.
