# Spec 04 — `atelier insights`

> Phase 1. The weekly summary that becomes a sticky habit.

## Why

Single-session reports (spec 02) are valuable. Weekly aggregates are what turn Atelier into a habit. Every Monday morning, a developer running `atelier insights` should see what they spent, where, and where the savings opportunities are.

This is the **conversion driver** to Pro tier: when a user sees "you spent $87 last week, $34 of which Atelier could have routed cheaper if you upgraded sync," they understand the value proposition.

## What — user-visible

```bash
$ atelier insights
Weekly insights · 2026-05-08 to 2026-05-15
─────────────────────────────────────────────────
Sessions:         42 (avg 38 min, total 26h 14m)
AI spend:         $124.62
Atelier savings:  $8.34  (6.7% of total)

Cost by vendor
  Anthropic     $98.20   79%  ████████████████░░░
  OpenAI        $19.40   16%  ███░░░░░░░░░░░░░░░░
  Google         $7.02    5%  █░░░░░░░░░░░░░░░░░░

Cost by tool (top 5)
  Edit          $34.21   27%
  Bash          $18.90   15%
  Agent         $12.55   10%
  Read           $8.04    6%
  Grep           $5.12    4%

Top spending sessions
  1. 7c2f8a    $14.20   "refactor cost_tracker"     4h 12m
  2. d4e1b2    $11.85   "implement compact bench"    3h 47m
  3. 9f3a05    $ 8.34   "fix routing tests"          2h 18m

Outcomes
  Route decisions: 312 (avg outcome_score 0.84)
  Compact events:  18  (avg outcome_score 0.79)
  Sessions hitting "extra_reads > 0.2": 4 — review compact aggression

Opportunities
  • 14 sessions had 30%+ Read turns — Gemini Flash would cost ~$3.10 less
  • 3 sessions hit Anthropic Edit retries — consider stronger sonnet routing
  • 92% of your spend stays on one machine — sync would help during travel
```

### Subcommands

```bash
atelier insights                         # last 7 days
atelier insights --since 30d
atelier insights --since 2026-05-01
atelier insights --json
atelier insights --vendor anthropic      # filter
atelier insights --group-by tool         # default; alternatives: vendor, model, session
```

## Where — files

| File | What changes |
|------|-------------|
| `src/atelier/infra/runtime/insights.py` | **New module.** Aggregates session reports. |
| `src/atelier/gateway/adapters/cli.py` | Add `insights` top-level command |
| `src/atelier/infra/runtime/session_state.py` | Add `list_sessions(*, since: datetime)` helper |
| `tests/infra/runtime/test_insights.py` | **New tests.** |

## Dependencies

This spec consumes:
- `SessionReport` from spec 02
- Outcome data from spec 01
- (Optional) Memory adapter from spec 03 to surface vendor inventory

If specs 01 and 02 are not yet shipped, this spec waits.

## Data model

```python
@dataclass
class InsightsWindow:
    since: datetime
    until: datetime
    session_count: int
    total_duration_seconds: float
    total_cost_usd: float
    total_atelier_savings_usd: float
    cost_by_vendor: dict[str, float]      # "anthropic" -> 98.20
    cost_by_tool: dict[str, float]
    cost_by_model: dict[str, float]
    top_sessions: list[SessionSummary]    # (session_id, cost, label, duration)
    outcomes_summary: OutcomesSummary
    opportunities: list[Opportunity]

@dataclass
class OutcomesSummary:
    route_decisions: int
    route_avg_score: float
    compact_events: int
    compact_avg_score: float
    sessions_with_high_extra_reads: list[str]

@dataclass
class Opportunity:
    kind: str          # "cross_vendor_route" | "compact_aggression" | "sync_value"
    message: str       # one-line user-facing description
    estimated_savings_usd: float
    sessions_affected: int
```

## Opportunity detection rules

Run these on the aggregated window:

1. **`cross_vendor_route`** — if >5 sessions have >30% Read-turn share AND the user is on a non-cheap vendor for those reads, estimate savings = read_token_count × (current_input_price − gemini_flash_input_price).
2. **`compact_aggression`** — if avg `compact.extra_read_rate > 0.15`, suggest tuning down compact aggression.
3. **`sync_value`** — if `len(distinct machines in window) > 1` AND `session.cross_machine_resume_count` > 0, suggest sync.
4. **`error_pattern`** — if a specific tool has model_error_rate >10% across 5+ sessions, suggest tighter routing for that tool.

Each rule outputs a single `Opportunity`. Rules with `estimated_savings_usd < $0.50` are suppressed to avoid noise.

## Render rules

- Default render: 80-column terminal, Unicode bars.
- Bars: `█` for full, `░` for empty, scaled to 20 chars max width.
- Costs always USD, two decimals.
- Time windows shown in user's local timezone.
- `--no-color` flag disables ANSI.
- `--json` flag dumps `InsightsWindow` as JSON.

## Out of scope

- **Cost forecasts.** Too speculative.
- **Per-engineer breakdown.** Spec 12 (Team).
- **Email digest delivery.** Future. Just CLI for now.
- **Comparison to previous period** ("you spent $20 more than last week"). Future spec.

## Acceptance criteria

- [x] `atelier insights` runs in <500ms with 200 sessions in window
- [x] Output matches the example layout
- [x] `--since 30d`, `--since 24h`, `--since 2026-05-01` all parse correctly
- [x] Empty window ("no sessions found in the last 7 days") renders without crash
- [x] `--json` produces valid JSON parseable by `jq`
- [x] Opportunities array contains at most 5 items, sorted by `estimated_savings_usd` desc
- [x] Unit tests with synthetic session data verify each opportunity rule fires correctly
- [x] Output uses no emoji (per repo convention)

## Open questions for the executor

1. Should weekly insights write a summary to `~/.atelier/insights/<weekstart>.json` so we have historical trends without recomputing? **Default: yes, write a cache, but always recompute on the fly when the user asks.**
2. The "session label" in top sessions — where does it come from? **Default: first user message of the session truncated to 40 chars. Add a `session_state.label` field if not present.**
3. How do we know which machine a session ran on for the `sync_value` opportunity? **Default: hostname captured in session_state. Add `machine_id` to session_state if not present.**

## Implementation order

1. `InsightsWindow` aggregator (pure compute over SessionReports)
2. Opportunity rules
3. Renderer
4. CLI command
5. Tests

## Status

- [ ] Pending
- [ ] In progress
- [x] Shipped
