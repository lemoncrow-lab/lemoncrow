# Spec 01 — Outcome Capture (Feedback Loop Foundation)

> Phase 1. Foundation. Everything downstream needs this.

## Why

Today Atelier makes routing and compaction decisions but never measures whether those decisions were *good*. We score by heuristic, then run, then forget. This spec adds the missing half: record the observable outcome of every decision 5–10 turns later. Without this, every other improvement to compact and route is opinion, not evidence.

## What — user-visible

No new top-level CLI. Two effects, both background:

1. After every route decision, Atelier records "what happened next" in a windowed outcome blob.
2. After every compact event, Atelier records the post-compact error drift and re-read rate.

The user sees this via `atelier insights` (spec 04) and `atelier session report` (spec 02). Direct access via:

```bash
atelier outcomes show <session-id>
atelier outcomes summary --since 7d
```

Both commands print JSON.

## Where — files

| File | What changes |
|------|-------------|
| `src/atelier/infra/runtime/outcome_capture.py` | **New module.** Schedulers + writers. |
| `src/atelier/infra/runtime/session_state.py` | Add `route_outcomes` and `compact_outcomes` arrays to the workspace state schema. |
| `src/atelier/gateway/adapters/mcp_server.py` | Wire `_emit_model_recommendation` and the compact path to call `outcome_capture.schedule(...)`. |
| `src/atelier/gateway/adapters/cli.py` | Add `outcomes` command group with `show` and `summary` subcommands. |
| `tests/infra/runtime/test_outcome_capture.py` | **New tests.** |

## Data model

Append-only entries in `session_state.json`. Stable schemas, additive only.

### Route outcome entry

```json
{
  "decision_id": "uuid",
  "at": "2026-05-15T10:30:00Z",
  "kind": "route",
  "tool": "Edit",
  "recommended_tier": "cheap",
  "recommended_model": "claude-haiku-4-5",
  "scored_state": {
    "turn_number": 23,
    "prior_errors": 1,
    "session_phase": "execution"
  },
  "outcome_window": {
    "captured_at": "2026-05-15T10:34:00Z",
    "turns_observed": 5,
    "model_errors_in_window": 1,
    "env_errors_in_window": 0,
    "retries_same_tool": 0,
    "extra_reads": 2,
    "outcome_score": 0.7
  }
}
```

### Compact outcome entry

```json
{
  "decision_id": "uuid",
  "at": "2026-05-15T10:30:00Z",
  "kind": "compact",
  "trigger": "utilisation_threshold",
  "tokens_before": 180000,
  "tokens_after": 95000,
  "must_keep_keywords": ["migration", "user_schema"],
  "outcome_window": {
    "captured_at": "2026-05-15T10:42:00Z",
    "turns_observed": 10,
    "error_drift": -0.05,
    "extra_read_rate": 0.10,
    "must_keep_violations": 0,
    "session_continued": true,
    "outcome_score": 0.82
  }
}
```

### outcome_score formula

For route:
```
outcome_score = 1.0
              - 0.4 × (1 if retries_same_tool > 0 else 0)
              - 0.3 × min(1.0, model_errors_in_window / 2)
              - 0.2 × min(1.0, extra_reads / 5)
clamped [0, 1]
```

For compact:
```
outcome_score = 1.0
              - 2.0 × max(0, error_drift)
              - 0.5 × extra_read_rate
              - 1.0 × (1 if must_keep_violations > 0 else 0)
clamped [0, 1]
```

These formulas mirror the existing benchmark logic so the dashboard numbers stay consistent.

## How the capture actually works

Synchronous capture is the wrong design — we'd block on every tool call. Instead:

1. At decision time, `outcome_capture.schedule(decision_id, kind, ...)` writes a *pending* entry to `session_state.json` with `outcome_window: null`.
2. After every subsequent ledger event for that session, a single hook `outcome_capture.advance(session_id)` is called.
3. `advance` walks all pending outcomes for the session, increments their "turns_observed", and if N is reached (5 for route, 10 for compact), computes the window stats and fills in `outcome_window`.
4. If the session ends before N turns, capture happens at session close with `turns_observed < N` (still valid, just less data).

This makes capture O(1) per turn, no background thread, no file locks beyond the existing `session_state.json` writer.

## Out of scope

- **Cross-session aggregation.** That's the federated learning spec (spec 11).
- **UI visualisation.** That's `atelier insights` (spec 04).
- **Multipliers self-tuning based on outcomes.** That's Phase 3.
- **Outcome capture for non-Atelier-decided turns.** We only score *our own* decisions.

## Acceptance criteria

- [x] `outcome_capture.schedule()` and `advance()` exist and are typed
- [x] `session_state.json` schema extended without breaking existing readers
- [x] Every call to `_emit_model_recommendation` results in a pending outcome
- [x] Every successful compact event results in a pending outcome
- [x] Outcomes populate `outcome_window` within N turns (verified in test)
- [x] `atelier outcomes show <id>` prints JSON for a session
- [x] `atelier outcomes summary --since 7d` aggregates outcome_scores by `(kind, tool)` and prints averages
- [x] Unit tests cover: empty session, session ending early, multiple overlapping outcomes
- [x] Zero net latency impact on tool calls (validated by benchmark before/after)

## Open questions for the executor

1. Should we capture outcomes even when the user IGNORED our recommendation (used a different model)? Argument for: tells us if our score was right anyway. Argument against: noisy. **Default decision: capture, but flag `recommendation_followed: bool`.**
2. Window size 5 (route) and 10 (compact) — should these be configurable? **Default decision: hardcoded for v1, configurable later.**
3. Where do outcomes live for sessions that span machines? **Defer to spec 06 (cross-machine sync).**

## Implementation order

1. Schema + writer (`outcome_capture.py`)
2. `session_state.json` migration
3. Wire `mcp_server.py`
4. CLI commands
5. Tests

## Status

- [x] Pending
- [x] In progress
- [x] Shipped — `src/atelier/infra/runtime/outcome_capture.py`, wired in `mcp_server.py` + `cli.py`, tests in `tests/infra/test_outcome_capture.py`
