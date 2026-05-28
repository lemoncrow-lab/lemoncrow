# Phase 1 Validation: Bench-Mode Toggle

## Requirements Coverage

| Requirement | Test Name | Type | Plan |
|-------------|-----------|------|------|
| MODE-01 (router passthrough) | `test_bench_mode_off_passthrough_router` | Unit | 01-03 |
| MODE-02 (compactor skip) | `test_bench_mode_off_passthrough_compactor` | Unit | 01-03 |
| MODE-03 (memory empty) | `test_bench_mode_off_memory_returns_empty` | Unit | 01-03 |
| MODE-04 (MCP tools hidden) | `test_bench_mode_off_mcp_tools_invisible` | Unit | 01-03 |
| MODE-05 (bootstrap + telemetry tag) | `test_bench_bootstrap_reads_env_once`, `test_bench_telemetry_tagged` | Unit | 01-03 |
| MODE-06 (separate ATELIER_ROOT) | `test_bench_arm_uses_separate_root` | Unit | 01-03 |
| MODE-07 (unit test coverage) | All unit tests above | Unit | 01-03 |
| MODE-08 (measurable token difference) | `test_bench_on_vs_off_token_counts_differ` | Integration | 01-03 |

## Verification Steps

1. `ATELIER_BENCH_MODE=off uv run atelier --version` — runs clean, no router/compactor invocation
2. `uv run pytest tests/core/test_bench_mode.py -q` — all unit tests pass
3. `uv run pytest tests/core/test_bench_mode_integration.py -q -m slow` — integration test shows `on_tokens > off_tokens`
4. `make lint && make typecheck` — no new violations
5. `uv run pytest -q -m "not slow"` — no regressions in full suite

## Pass Criteria

- All 8 MODE requirements covered by at least one test
- `mcp_tool_visible_to_llm` returns False for all tools when bench-off (even with `ATELIER_DEV_MODE=1`)
- `compress_with_sleeptime()` never fires LLM/SQLite writes when bench-off
- `session_start` telemetry event carries `bench_mode` field
- `make_arm_env()` returns isolated env dict with correct `ATELIER_ROOT` and `ATELIER_BENCH_MODE`
