# P5 — Trace + telemetry integration

> Depends on: P1, P3.
> Unblocks: P6 (replays read these traces).

## Goal

Every `compile()` and every downstream LLM call records enough data to
prove caching is firing — or to find the cache breaker when it isn't.
Without P5, this feature has no economic story.

## Files

```
src/atelier/core/capabilities/prompt_compilation/
    trace.py
src/atelier/core/service/telemetry/
    emit.py          (extend; add `prompt_compile` event)
src/atelier/infra/storage/
    migrations/00XX_prompt_compilations.sql
tests/core/capabilities/prompt_compilation/
    test_trace.py
tests/core/service/telemetry/
    test_prompt_compile_event.py
```

## Trace row schema

```python
@dataclass(frozen=True)
class PromptCompilationTrace:
    trace_id: str
    session_id: str | None
    provider: Provider | None
    model: str | None

    stable_prefix_hash: str
    stable_prefix_tokens: int
    dynamic_tail_tokens: int
    total_input_tokens: int

    # Filled later (on response parse) — see “LLM-side fields” below.
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_hit_rate: float | None = None

    cache_lint_score: int
    cache_breakers: tuple[str, ...]

    estimated_cached_savings_usd: float | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
```

## DB table

```sql
CREATE TABLE IF NOT EXISTS prompt_compilations (
    trace_id                       TEXT PRIMARY KEY,
    session_id                     TEXT,
    provider                       TEXT,
    model                          TEXT,
    stable_prefix_hash             TEXT NOT NULL,
    stable_prefix_tokens           INTEGER NOT NULL,
    dynamic_tail_tokens            INTEGER NOT NULL,
    total_input_tokens             INTEGER NOT NULL,
    cached_input_tokens            INTEGER,
    cache_write_tokens             INTEGER,
    cache_read_tokens              INTEGER,
    cache_hit_rate                 REAL,
    cache_lint_score               INTEGER NOT NULL,
    cache_breakers                 TEXT NOT NULL,    -- JSON array
    estimated_cached_savings_usd   REAL,
    created_at                     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_prompt_compilations_session
    ON prompt_compilations (session_id, created_at);
CREATE INDEX IF NOT EXISTS ix_prompt_compilations_prefix_hash
    ON prompt_compilations (stable_prefix_hash);
```

The prefix-hash index is the load-bearing one: it lets the dashboard
group traces that share a prefix and compute hit rate cheaply.

## Two-phase trace

1. **At compile time** — capability writes the row with everything it
   knows: hash, lint score, token counts, lint breakers. `cache_read_tokens`
   etc. are null.
2. **At LLM-response time** — the host (or the SDK helper in P8) calls
   `compiler.attach_usage(trace_id, raw_usage_dict)`. The capability
   normalizes the provider's usage payload via the corresponding
   `providers_*.parse_usage` and updates the row.

This decoupling is important: Atelier never sees the network call. It
just gets the usage payload after the fact.

## Cost math

```
savings_usd = cache_read_tokens * (input_price - cached_input_price)
```

Prices come from `core/capabilities/pricing.py`. The function lives in
`trace.py` so other capabilities can reuse it.

## Scorecard rows

Add the following to `docs/quality/scorecard.md`:

| Metric | Target |
|---|---|
| Compiler-stamped traces with non-null prefix hash | 100% |
| Stable prefix hash collision rate within a session | ≥80% (i.e. ≥80% of turns share a prefix with the previous turn) |
| Cache hit rate (where vendor reports it) | ≥60% |
| Cache lint score (median) | ≥85 |
| Estimated USD savings per coding session | track; no fixed target until we have baseline |

## Tests

- `test_trace.py::test_row_written_on_compile`.
- `test_trace.py::test_attach_usage_updates_row`.
- `test_trace.py::test_savings_math_matches_pricing`.
- `test_trace.py::test_attach_usage_idempotent`.
- `test_prompt_compile_event.py::test_event_emitted_on_substrate`.

## Acceptance

- `uv run pytest tests/core/capabilities/prompt_compilation tests/core/service/telemetry -q` passes.
- A demo script (`examples/prompt_compiler/demo.py`) compiles two
  prompts in a row, prints the prefix-hash overlap, and shows the trace
  rows in SQLite.

## Out of scope

- Frontend dashboard. The data lands in SQLite; the existing dashboard
  team can pick it up. (See `docs/quality/scorecard.md` for the
  intended visual.)
