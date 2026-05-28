---
phase: 02-terminalbench-adapter
plan: "02"
subsystem: benchmarks
tags: [terminalbench, adapter, docker, claude-code, bench-mode]
dependency_graph:
  requires:
    - 02-01 (terminalbench foundation: TaskSpec, RunConfig, tasks.yaml)
    - src/atelier/bench/mode.py (make_arm_env, BenchMode)
    - terminal_bench.agents.installed_agents.abstract_installed_agent
  provides:
    - AtelierClaudeAgent (AbstractInstalledAgent subclass)
    - parse_stream_jsonl (stream-json NDJSON parser)
    - run_terminalbench_trial (end-to-end trial orchestration)
    - AdapterResult (TB-04 transcript dataclass)
    - RunRecord + write_records + write_transcript (JSONL/JSON writers)
    - render_run_summary + render_mode_comparison (ANSI reporter)
    - setup.sh.j2 (Docker agent install template)
  affects:
    - 02-03 (Wave 3 analysis will consume AdapterResult + runs.jsonl)
tech_stack:
  added: []
  patterns:
    - AbstractInstalledAgent subclass pattern for TerminalBench Docker integration
    - Minimal env dict forwarding (T-02-04 threat mitigation)
    - NDJSON line-by-line parsing with graceful error handling
    - Atomic JSON transcript write via os.replace()
    - TYPE_CHECKING guard for lazy import to prevent circular dependency
key_files:
  created:
    - benchmarks/terminalbench/agent_adapter.py
    - benchmarks/terminalbench/runner.py
    - benchmarks/terminalbench/reporter.py
    - benchmarks/terminalbench/setup.sh.j2
  modified: []
decisions:
  - "parse_stream_jsonl returns mapped key names (cost_usd, latency_ms, latency_api_ms) not raw JSON names — matches CRITICAL spec in prompt"
  - "AtelierClaudeAgent._env is a MINIMAL dict (not full os.environ) — prevents host dev contamination per T-02-04"
  - "reporter.py uses TYPE_CHECKING + string annotation for AdapterResult import — avoids circular dep with runner.py"
metrics:
  duration: "~5 minutes"
  completed: "2026-05-28T17:21:25Z"
  tasks_completed: 2
  tasks_total: 2
  files_created: 4
  files_modified: 0
---

# Phase 2 Plan 02: TerminalBench Adapter (Wave 2) Summary

**One-liner:** AtelierClaudeAgent wires bench-mode into TerminalBench Docker via stream-json tee; runner CLI drives ON/OFF trials and writes atomic transcript JSON with ANSI reporter.

## What Was Built

### Task 1: `agent_adapter.py` + `setup.sh.j2`

**`benchmarks/terminalbench/agent_adapter.py`** — Core integration module:

- **`AtelierClaudeAgent(AbstractInstalledAgent)`** — TerminalBench agent subclass that runs `claude --verbose --output-format stream-json` inside Docker, tee-ing to `/agent-logs/stream.jsonl`. Minimal `_env` property forwards only `ANTHROPIC_API_KEY`, `ATELIER_BENCH_MODE`, `FORCE_AUTO_BACKGROUND_TASKS`, `ENABLE_BACKGROUND_TASKS`, and optionally `ANTHROPIC_MODEL`. `ATELIER_DEV_MODE` explicitly excluded (T-02-04).
- **`parse_stream_jsonl(log_path)`** — Reads NDJSON line-by-line, finds last `"type": "result"` line, extracts token/cost/latency fields. Returns `{"error": "no_result_line"}` with zeros when absent. Key names: `cost_usd`, `latency_ms`, `latency_api_ms` (mapped from raw JSON names).
- **`run_terminalbench_trial()`** — Instantiates `Harness` with `agent_import_path="terminalbench.agent_adapter:AtelierClaudeAgent"` (colon separator required), snapshots/injects arm env via `make_arm_env()`, restores env in finally block, assembles full `AdapterResult` from `TrialResults` + parsed stream log.
- **`AdapterResult`** — `@dataclass` with all 25 TB-04 schema fields + `to_dict()`.
- **`CONTAINER_STREAM_LOG = "/agent-logs/stream.jsonl"`**

**`benchmarks/terminalbench/setup.sh.j2`** — Jinja2 Docker install template: apt → nvm v0.40.2 → Node 22 → `@anthropic-ai/claude-code@{{ version | default('latest') }}`.

### Task 2: `runner.py` + `reporter.py`

**`benchmarks/terminalbench/runner.py`** — CLI runner:

- **`RunRecord`** — `@dataclass` with all `AdapterResult` fields + `transcript_path`; `to_jsonl()` for JSONL serialisation.
- **`write_records(rows, path)`** — JSONL writer.
- **`write_transcript(result, out_dir)`** — Atomic JSON writer via `os.replace()` (T-02-08). Filename: `<task_id>__<mode>__rep<N>.json`.
- **`main()`** — argparse CLI: `--task`, `--mode {on,off}`, `--model`, `--rep`, `--out`, `--dataset-name`, `--dataset-version`. Calls `run_terminalbench_trial()`, writes transcript + JSONL, prints ANSI summary.

**`benchmarks/terminalbench/reporter.py`** — ANSI reporter (no rich/click):

- **`render_run_summary(result)`** — Colour-coded verdict + token/cost/latency metrics.
- **`render_mode_comparison(on_result, off_result)`** — Side-by-side table with green/red delta row.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Renamed parse_stream_jsonl return keys to mapped names**
- **Found during:** Final verification
- **Issue:** Plan task spec said return `total_cost_usd`/`duration_ms`/`duration_api_ms` but prompt's CRITICAL implementation details and final verification assert `cost_usd`/`latency_ms`/`latency_api_ms`
- **Fix:** Changed `_ZERO_RESULT` and return dict to use `cost_usd`, `latency_ms`, `latency_api_ms`; updated `run_terminalbench_trial` to use new key names
- **Files modified:** `benchmarks/terminalbench/agent_adapter.py`
- **Commit:** f9d1908

**2. [Rule 1 - Lint] ruff MINUS SIGN / black formatting auto-fixes**
- **Found during:** Pre-commit hook on each commit
- **Fix:** Replaced Unicode minus `−` with ASCII `-` in reporter.py delta row label; staged reformatted files and re-committed
- **Files modified:** `benchmarks/terminalbench/reporter.py`, `benchmarks/terminalbench/agent_adapter.py`

## Known Stubs

None — all exported symbols are fully implemented. `run_terminalbench_trial` requires a live Docker + TerminalBench environment to execute an actual trial but is not a stub; it gracefully handles exceptions via `claude_error` field.

## Threat Flags

None — no new network endpoints, auth paths, or schema changes beyond those in the plan's threat model (T-02-04 through T-02-08 all mitigated as specified).

## Self-Check: PASSED

Files exist:
- ✅ `benchmarks/terminalbench/agent_adapter.py`
- ✅ `benchmarks/terminalbench/runner.py`
- ✅ `benchmarks/terminalbench/reporter.py`
- ✅ `benchmarks/terminalbench/setup.sh.j2`

Commits exist:
- ✅ b356460 — feat(tb-02): implement AtelierClaudeAgent adapter
- ✅ acef0f9 — feat(tb-04,tb-05): implement runner CLI and ANSI reporter
- ✅ f9d1908 — fix(tb-02): rename parse_stream_jsonl keys
