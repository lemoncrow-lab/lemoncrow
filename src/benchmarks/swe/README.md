# SWE-bench harness for Atelier

A standalone benchmark module that compares **vanilla coding agents** against
**Atelier-instrumented agents** across resolve rate, cost, tokens, turns,
time, tool-calls and Atelier-specific metrics (ReasonBlock hits, monitor
events, rescue events, rubric verdicts).

> Inspired by ReasonBlocks (runtime monitors / reasoning library), Lemma
> (failure clusters), Rubric (codebase-specific verification), and SWE-bench
> (real GitHub issues).

## Layout

```
benchmarks/swe/
├── README.md          ← you are here
├── run_swe_bench.py   ← Click entry: `atelier benchmark swe ...`
├── config.py          ← strict pydantic config schema
├── datasets.py        ← swe_bench_lite / verified / mock / custom JSONL
├── task_runner.py     ← one (task × mode × attempt) → metrics row
├── agent_runner.py    ← agent abstraction + offline MockAgent
├── modes.py           ← 5 modes (vanilla → warm reasonblocks)
├── metrics.py         ← RunMetrics, JSONL writer/reader, aggregate
├── patch_export.py    ← write SWE-bench predictions JSONL
├── swebench_eval.py   ← shell out to swebench harness if installed
├── report.py          ← markdown + JSON benchmark report
├── prompts.py         ← per-mode system prompts
├── configs/
│   ├── lite_20.yaml
│   ├── lite_100.yaml
│   └── verified_100.yaml
└── outputs/           ← gitignored except .gitkeep
```

## Modes

| Mode | MCP exposed | Forced workflow | Runtime (ledger / monitors / compressor / smart tools / savings) | Warm RBs |
|---|---|---|---|---|
| `vanilla` | – | – | – | – |
| `atelier_tools_available` | ✓ | – | – | – |
| `atelier_forced_workflow` | ✓ | get_context · rescue_failure · run_rubric_gate · record_trace | – | – |
| `atelier_full_runtime` | ✓ | ✓ | ✓ | – |
| `atelier_warm_reasonblocks` | ✓ | ✓ | ✓ | preloaded from calibration |

`show-modes` prints the matrix at runtime:

```bash
uv run atelier benchmark swe show-modes
```

## Quickstart (offline, no API keys)

```bash
# 20-task mock run (uses built-in mock dataset + MockAgent)
uv run atelier benchmark swe run --config benchmarks/swe/configs/lite_20.yaml

# Output:
#   benchmarks/swe/outputs/lite_20/<TIMESTAMP>/
#     config.snapshot.json
#     metrics_<mode>.jsonl
#     predictions_<mode>.jsonl   ← SWE-bench format
#     patches/<instance_id>.<mode>.patch
#     report.md
#     report.json

# Score predictions (mock evaluator if `swebench` not installed)
uv run atelier benchmark swe evaluate --run-dir benchmarks/swe/outputs/lite_20/<TS> --mock

# Re-render report from existing metrics
uv run atelier benchmark swe report --run-dir benchmarks/swe/outputs/lite_20/<TS>
```

## Make targets

```bash
make swe-bench-lite-20      # tiny smoke
make swe-bench-lite-100     # full lite slice
make swe-bench-verified-100 # verified slice (requires warm calibration)
make swe-bench-report DIR=benchmarks/swe/outputs/lite_20/<TS>
```

## Datasets

* `swe_bench_lite` / `swe_bench_verified` — auto-loaded via the optional
  `datasets` (HuggingFace) package; falls back to a built-in mock so unit
  tests work offline.
* Custom JSONL — set `custom_tasks_path: path/to/tasks.jsonl` (one record
  per line; must include `instance_id`, `repo`, `base_commit`,
  `problem_statement`).

### Safety

* The agent payload **never** contains `patch`, `test_patch`,
  `FAIL_TO_PASS`, or `PASS_TO_PASS` — the harness asserts this in
  `task_runner.run_one`.
* `atelier_warm_reasonblocks` requires `warm_reasonblocks_path`; the run
  command refuses to start otherwise. Calibration trace IDs **must** be
  disjoint from eval task IDs (you own the split — the harness records the
  warm-blocks path in `config.snapshot.json` for audit).
* `seed` and the full config snapshot are persisted next to every report.

## Predictions format

Each `predictions_<mode>.jsonl` contains one record per task:

```json
{"instance_id": "django__django-12345",
 "model_name_or_path": "claude:claude-sonnet-4.6:atelier_forced_workflow",
 "model_patch": "--- a/...\n+++ b/...\n@@ ..."}
```

This is the schema accepted by
`python -m swebench.harness.run_evaluation`.

## Running the official evaluator

```bash
uv pip install swebench
uv run atelier benchmark swe evaluate --run-dir benchmarks/swe/outputs/lite_20/<TS>
```

If `swebench` is missing the command prints exact install instructions and
falls back to the dependency-free mock evaluator (text-equality vs gold
patch — useful for harness self-tests, **not** a publishable number).

## Adding a real host adapter

`agent_runner.build_agent()` returns a `MockAgent` for `agent_host: mock`
and a `_UnsupportedAgent` stub for everything else. Wire a new host by:

1. Implementing the `Agent` protocol (`solve(task, mode_spec, cfg) → AgentResult`).
2. Returning workflow events (`{"event": "record_trace", "status": "ok"}`,
   `{"event": "reasonblock_hit", "block_id": "..."}` etc.) so report
   counters populate.
3. Registering it in `build_agent()`.

## Tests

```bash
uv run pytest -q tests/test_swe_benchmark_harness.py
```

Covers: config loads, mock dataset runs, mock agent produces a patch,
metrics written, predictions JSONL valid, report generated, modes are
distinct, no gold-patch leakage, warm ReasonBlocks gate enforced,
swebench-eval skips cleanly when the package is absent.
