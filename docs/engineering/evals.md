# Evals

Atelier includes an eval system for tracking known-good agent behaviors and catching regressions.

## What Evals Are

An eval case is a recorded scenario: given this task + domain + plan, the expected outcome is X. Evals are created from real traces (via `eval-from-cluster`) or manually.

Evals are different from unit tests: they test agent-facing behavior (plan check, context retrieval, rubric gating) rather than internal logic. The pytest suite (`make test`) tests internal logic.

## Creating Evals

### From a failure cluster

```bash
# 1. Identify a failure cluster
atelier failure list

# 2. Create an eval case from it
atelier eval-from-cluster CLUSTER_ID --save
```

### Manually

```bash
atelier eval list         # see existing format
# then add via service API or direct store manipulation
```

## Running Evals

```bash
atelier eval run [--domain TEXT] [--eval-id ID]
```

Or via the benchmark command (runs all active evals):

```bash
atelier benchmark run
atelier benchmark run --json
```

## Eval Lifecycle

```
candidate → active → deprecated
```

| State        | Description                                |
| ------------ | ------------------------------------------ |
| `candidate`  | Extracted from a cluster, not yet verified |
| `active`     | Promoted, counts toward benchmark          |
| `deprecated` | Retired, no longer runs                    |

Promote an eval case:

```bash
atelier eval promote EVAL_ID
```

Deprecate when a pattern is no longer relevant:

```bash
atelier eval deprecate EVAL_ID
```

## Eval Format

Eval cases are stored in `.atelier/evals/` as JSON files:

```json
&#123;
  "id": "eval_state_change_slug_deadend",
  "domain": "state.change",
  "task": "Apply a live state change",
  "plan_steps": ["Resolve target from URL slug alone", "Apply the change"],
  "expected_check_plan_status": "blocked",
  "expected_warnings_include": ["dead end: resolve target from url slug alone"],
  "status": "active",
  "created_at": "2026-04-21T00:00:00Z"
&#125;
```

## Benchmark Output

```bash
atelier benchmark run
```

Example output:

```
eval suite: 12 active cases
  ✓ eval_shopify_handle_deadend       (check-plan: blocked ✓)
  ✓ eval_pdp_schema_gid_required      (check-plan: blocked ✓)
  ✓ eval_shopify_gid_plan_passes      (check-plan: pass ✓)
  ...

12/12 passed
```

Failed evals indicate a regression in block/rubric/environment data or the runtime logic.

## Makefile

```bash
make verify   # includes eval run via pytest
```

The pytest suite in `tests/test_golden_fixtures.py` covers golden fixture scenarios that overlap with evals.
