---
id: WP-18
title: Refactor `Savings.tsx` to show per-lever breakdown
phase: E
pillar: 3
owner_agent: atelier:code
depends_on: [WP-14]
status: done
---

# WP-18 — Savings page refactor

## Why

The current `Savings.tsx` displays a single aggregate number. With per-lever telemetry from WP-14,
we can show a lever-level breakdown that proves the >50 % claim is real and lets us spot regression
in any one lever.

## Files touched

- `frontend/src/pages/Savings.tsx` — edit
- `frontend/src/components/LeverBar.tsx` — new
- `frontend/src/components/SavingsTimeChart.tsx` — new
- `tests/gateway/test_savings_api.py` — new (server side, ensures the API returns per-lever)

## How to execute

1. Server endpoint: extend `/v1/savings/summary` to return:

   ```json
   &#123;
     "window_days": 7,
     "total_naive_tokens": 412000,
     "total_actual_tokens": 198000,
     "reduction_pct": 51.9,
     "per_lever": &#123;
       "search_read": 21000,
       "batch_edit": 14500,
       "ast_truncation": 27000,
       "sleeptime": 18000,
       "cached_read": 11000,
       "scoped_recall": 9000,
       "compact_lifecycle": 8500,
       "reasonblock_inject": 5500
     &#125;,
     "by_day": [&#123; "day": "2026-04-26", "naive": 60000, "actual": 28000 &#125;, ...]
   &#125;
   ```

2. UI:
   - Top KPI: large `reduction_pct` with sparkline
   - Per-lever bar chart (sorted descending) using the new `LeverBar` component (no chart lib;
     pure CSS bars to keep bundle small)
   - 14-day stacked area chart (`SavingsTimeChart`) — use existing chart lib if one is already in
     `package.json`; otherwise SVG hand-rolled
    - "Why this matters" callout linking to the V2 plan

3. Empty state: when no savings data has been recorded yet, render a coaching message:
   "Run any task with `atelier-mcp` enabled to start collecting savings telemetry."

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/gateway/test_savings_api.py -v

cd frontend
npm run typecheck -- --noEmit
npm test -- --watchAll=false src/pages/Savings.test.tsx
```

## Definition of done

- [x] API returns per-lever breakdown
- [x] Chart renders both populated and empty states
- [x] No new heavy chart library added
- [x] All tests pass
- [x] `INDEX.md` updated; trace recorded
