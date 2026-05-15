# Spec 02 — Per-Session Cost Report

> Phase 1. The honest dashboard, command-line edition.

## Why

Every developer paying $50–500/mo on AI tools wants the same thing: a real, trustworthy view of where the money went. Atelier already has the ledger and cost tracker. This spec turns them into a developer-facing command.

This is the first feature a Free-tier user sees. It has to be **instantly compelling** — show real numbers, real attributions, in under 1 second.

## What — user-visible

```bash
$ atelier session report
Session 7c2f8a (last 4h 12m)
─────────────────────────────────────────────────
Vendor:           Anthropic
Models used:      claude-sonnet-4.6 (89 turns), claude-haiku-4-5 (3 turns)
Total turns:      92
Tool calls:       287

Cost breakdown
  Input tokens:   1,283,440  →  $3.85
  Cache writes:    487,200   →  $1.46
  Cache reads:   2,184,560   →  $0.66
  Output tokens:    98,440   →  $1.48
  ─────────────────────────
  Total:                       $7.45

Atelier savings
  Routing recommendations: 7 turns downtiered → saved $0.18
  Compaction (1 event):    freed 84,000 tok → avoided ~$0.32 in resend cost
  Total saved this session:                    $0.50

Top 5 costliest tools this session
  Edit          12 calls   $1.82
  Bash          43 calls   $0.94
  Read         108 calls   $0.71
  Grep          34 calls   $0.34
  Agent          2 calls   $0.28
```

### Subcommands

```bash
atelier session report                    # current / most recent session
atelier session report <session-id>
atelier session list                      # last 20 sessions, costs, durations
atelier session list --since 7d           # filter
atelier session list --json               # machine-readable
```

## Where — files

| File                                            | What changes                                                                                  |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `src/atelier/gateway/adapters/cli.py`         | Add `session` command group with `report` and `list` subcommands                        |
| `src/atelier/infra/runtime/session_report.py` | **New module.** Pure compute: ledger → report data.                                    |
| `src/atelier/infra/runtime/cost_tracker.py`   | Extend to expose `per_tool_cost_breakdown(session_id)`                                      |
| `src/atelier/infra/runtime/run_ledger.py`     | Add `RunLedger.duration_seconds`, `RunLedger.first_event_at`, `RunLedger.last_event_at` |
| `tests/infra/runtime/test_session_report.py`  | **New tests.**                                                                          |

## Data model

`SessionReport` is a pure dataclass; the CLI formats it for terminal or JSON.

```python
@dataclass
class SessionReport:
    session_id: str
    started_at: datetime
    ended_at: datetime | None  # None = still running
    duration_seconds: float
    vendor: str                # "Anthropic" | "OpenAI" | "Google" | "mixed"
    models_used: dict[str, int]  # model name → turn count
    total_turns: int
    tool_call_count: int

    # Costs
    input_token_cost_usd: float
    cache_write_cost_usd: float
    cache_read_cost_usd: float
    output_token_cost_usd: float
    total_cost_usd: float

    # Tokens
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int

    # Atelier savings (read from outcome_capture + ledger)
    routing_downtiered_turns: int
    routing_savings_usd: float
    compact_events: int
    compact_savings_estimate_usd: float
    total_atelier_savings_usd: float

    # Top tools
    top_tools_by_cost: list[tuple[str, int, float]]  # (name, count, cost)
```

## Render rules

- Terminal output uses Unicode box-drawing for clean tables.
- Numbers right-aligned. Costs always show two decimal places.
- Token counts use thousands separators.
- All costs in USD; later spec adds currency conversion (out of scope for v1).
- `--json` flag dumps the dataclass as JSON (using `dataclasses.asdict` + `default=str` for datetimes).
- `--no-color` flag disables ANSI codes.

## What counts as "the session"

A session is one entry in `~/.atelier/workspaces/<hash>/runs/<session-id>.json`. The Atelier ledger already tracks this. We are not re-parsing native CLI logs for this spec — we use what's in our own ledger.

## Out of scope

- **Cross-vendor counterfactual** ("what would Gemini have cost?") — spec 07
- **Per-engineer attribution** — spec 12 (Team tier)
- **Historical charts** — terminal-only for v1; web in spec 10
- **Cost forecasts** — too speculative for v1

## Acceptance criteria

- [X] `atelier session report` runs in under 200ms on a session with 100 turns
- [X] Output matches the example above for a real session (within ±$0.01)
- [X] `--json` flag produces valid JSON parseable by `jq`
- [X] `atelier session list` shows last 20 sessions sorted by `started_at` desc
- [X] `--since 7d` and `--since 24h` work
- [X] Zero-cost sessions (e.g., synthetic-only) render with `$0.00`, no crashes
- [X] Sessions still running render with `duration_seconds: (ongoing)`
- [X] Unit tests cover: empty session, multi-model session, session with no tool calls

## Open questions for the executor

1. Pricing source: today we have `get_model_pricing(model)`. Confirm it covers haiku, sonnet, opus, gpt-4o, gpt-4o-mini, gemini-pro, gemini-flash. If not, extend it in this spec.
2. For "compact savings estimate" — should we count tokens freed × current model's input price? Or use a $-savings formula tied to context-resend avoidance? **Default: tokens freed × input price.**
3. If the user runs `atelier session report` with no arg and there's no recent session, what do we show? **Default: helpful "no sessions found — run any AI command first" message.**

## Implementation order

1. Extend `cost_tracker.per_tool_cost_breakdown()`
2. Build `session_report.build_report(ledger) -> SessionReport`
3. Renderer (`SessionReport.render_text()` and `.render_json()`)
4. CLI commands
5. Tests

## Status

- [X] Pending
- [X] In progress
- [X] Shipped
