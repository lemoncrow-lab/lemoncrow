---
id: WP-14
title: ContextBudget recorder + Prometheus metric
phase: C
pillar: 3
owner_agent: atelier:code
depends_on: [WP-02]
status: done
---

# WP-14 — Context budget telemetry

## Why

Pillar 3's >50% claim is only credible with per-turn measurements. This packet ships the
`ContextBudget` recorder and the `atelier_tokens_saved_total&#123;lever&#125;` Prometheus metric so the
benchmark harness (WP-19) and the dashboard (WP-18) have data to read.

## Files touched

- `src/atelier/core/foundation/savings_models.py` — already created in WP-02, edit if needed
- `src/atelier/core/capabilities/telemetry/context_budget.py` — new
- `src/atelier/gateway/adapters/mcp_server.py` — edit (call recorder around every tool dispatch)
- `src/atelier/core/service/api.py` — edit (`/metrics` endpoint already serves Prometheus)
- `tests/core/test_context_budget_recorder.py`
- `tests/infra/test_savings_metric_increments.py`

## How to execute

1. Implement `ContextBudgetRecorder`:

   ```python
   class ContextBudgetRecorder:
       def __init__(self, store): ...
       def record(self, *, session_id, turn_index, model, input_tokens, cache_read_tokens,
                  cache_write_tokens, output_tokens, naive_input_tokens,
                  lever_savings: dict[str, int], tool_calls: int) -> None: ...
       def aggregate_run(self, session_id: str) -> RunSavings: ...
   ```

2. In `mcp_server.py`, wrap the dispatch loop:
   - Before tool exec: snapshot prompt tokens
   - After tool exec: compute lever-attributed savings (each capability now returns its own
     `tokens_saved` field — see WP-04, WP-09, WP-10, WP-11, WP-21, WP-23)
   - Emit Prometheus counter `atelier_tokens_saved_total&#123;lever, model&#125;`
   - Persist a `ContextBudget` row

3. The `naive_input_tokens` value in production is **estimated** as `input_tokens + sum(lever_savings)`.
   The benchmark harness (WP-19) replays without Atelier to compute the real baseline.

4. Tests:
   - Recorder unit test
   - Integration: run a fake tool dispatch loop, assert metric value increments and DB rows exist

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/core/test_context_budget_recorder.py \
                     tests/infra/test_savings_metric_increments.py -v
make verify
```

## Definition of done

- [x] Recorder shipped, called on every tool dispatch
- [x] Prometheus metric exposed at `/metrics`
- [x] DB rows persisted; query via `atelier savings list --session-id ...`
- [x] `make verify` green
- [x] `INDEX.md` updated; trace recorded

## Implementation summary (WP-14)

**Status**: ✅ Complete

### Files created

1. **src/atelier/core/capabilities/telemetry/context_budget.py** — `ContextBudgetRecorder` and `RunSavings` classes
   - Records per-turn metrics: model, token counts, cache usage, lever-attributed savings
   - Aggregates runs to compute total savings by lever
   - Emits Prometheus metrics via `prometheus_client.Counter`

2. **tests/core/test_context_budget_recorder.py** — 7 unit tests
   - Record single and multiple turns
   - Aggregation of metrics
   - Serialization/deserialization
   - Per-ID retrieval

3. **tests/infra/test_savings_metric_increments.py** — 7 integration tests
   - Simulated dispatch loop with metric accumulation
   - Database schema migration verification
   - Unique constraint enforcement
   - Prometheus counter increments (if available)
   - Large run handling (50+ turns)
   - JSON serialization of lever_savings
   - Index performance verification

### Files modified

1. **src/atelier/core/foundation/store.py** — Added three methods to `ReasoningStore`:
   - `persist_context_budget(record)` — Insert/replace a ContextBudget record
   - `list_context_budgets(session_id)` — Query all records for a run, ordered by turn_index
   - `get_context_budget(cb_id)` — Retrieve a single record by ID

2. **src/atelier/gateway/adapters/mcp_server.py** — Added context budget recording hook:
   - Global `_context_budget_recorder` singleton
   - `_get_context_budget_recorder()` factory function
   - `_record_context_budget_for_tool()` helper to emit metrics on tool dispatch
   - Integrated call in the `tools/call` handler (line ~845)
   - Graceful fallback if Prometheus not available

3. **src/atelier/core/service/api.py** — Enhanced `/metrics` endpoint:
   - Collects metrics from `prometheus_client.REGISTRY` if available
   - Returns Prometheus text format (`text/plain`)
   - Falls back to JSON if Prometheus not installed

### Database

- Existing migration `v2_003_context_budget.sql` was already in place
- Schema includes:
  - `context_budget` table with columns for all token counts, lever_savings (JSON), tool_calls
  - Unique constraint on `(session_id, turn_index)`
  - Index on `session_id` for efficient per-run queries

### Test results

- **14 tests passing** (100%)
  - 7 unit tests for ContextBudgetRecorder
  - 7 integration tests for full dispatch loop and DB persistence
- **No regressions** in existing tests
- All acceptance criteria met
