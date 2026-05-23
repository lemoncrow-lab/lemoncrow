# Deferred Items — Phase 04 Historical Code Intelligence

## 04-04 Execution

- **Resolved typecheck debt:** `make typecheck` now passes after `8fa8629`, so the earlier Phase 4 strict-mypy blocker is closed.
- **Pre-existing test debt:** Broad repo tests still fail outside Phase 4; the first reproduced failure remains `tests/benchmarks/code_intel/test_call_graph_bench.py`, which is earlier-phase call-graph debt rather than a Phase 4 blocker.
