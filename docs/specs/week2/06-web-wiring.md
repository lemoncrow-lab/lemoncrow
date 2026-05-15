# Spec 06 — Web Wiring for Week-2 Specs

> Phase 1, final piece. Closes the gap between shipped backend and the user's eyes.

## Why

Specs 01–05 are shipped on the backend with passing tests and working CLI commands. None of them appear in the web UI. The Phase 1 launch claim is "see your AI costs across vendors" — that promise needs to be visible in **both** terminal and web. Without this spec, a user who installs Atelier and opens the dashboard sees the old `Traces`, `Savings`, and knowledge-block `Memory` pages but none of the new value.

This spec is intentionally narrow: it adds the FastAPI routes and React pages needed to surface specs 01–05. No new logic, no new datatypes — just wiring.

## What — user-visible

After this ships, the dashboard's left nav looks like:

```
Overview
Sessions          ← new, replaces old Traces-as-Sessions
  ↳ Session detail (cost report)
Traces            ← old page, moved
Memory            ← cross-vendor memory facts (new content)
Insights          ← rebuilt to show WeeklyInsights from spec 04
Outcomes          ← new, shows route + compact outcome scores
Reports           ← new, lists published benchmark reports
Savings
Watchdogs
Hosts
Tools
Agents
Telemetry         ← renamed from "Insights" to disambiguate
Analytics
External
Optimizations
```

Per page:

| Page | Backend source | Renders |
|------|---------------|---------|
| **Sessions** (`/sessions`) | `GET /v1/sessions?since=7d` | Table of recent sessions (cost, duration, vendor, models) |
| **Session detail** (`/sessions/<id>`) | `GET /v1/sessions/<id>` | Full `SessionReport` dataclass — cost breakdown, top tools, Atelier savings |
| **Memory** (`/memory`) | `GET /v1/memory/facts` | Cross-vendor memory facts grouped by vendor (Spec 03). Existing knowledge-block view becomes a tab or sub-page. |
| **Insights** (`/insights`) | `GET /v1/insights?since=7d` | `InsightsWindow` from Spec 04 — weekly cost, top sessions, opportunities |
| **Outcomes** (`/outcomes`) | `GET /v1/outcomes/summary?since=7d` | Average route + compact outcome scores, sessions with high extra_reads |
| **Reports** (`/reports`) | `GET /v1/reports` + `GET /v1/reports/<week>` | List of published weekly benchmark reports + per-week markdown viewer |

The old `Traces` page moves to `/traces`. The old `Insights` page (telemetry config) renames to **Telemetry** at `/telemetry`.

## Where — files

### Backend (`src/atelier/core/service/api.py`)

Add the following routes near the existing `/v1/*` block. All require `Depends(verify_api_key)`.

| Method | Path | Returns | Module called |
|--------|------|---------|---------------|
| `GET` | `/v1/sessions` | `list[SessionSummary]` | `session_report.list_sessions(since)` |
| `GET` | `/v1/sessions/{id}` | `SessionReport` | `session_report.build_report(load_ledger(id))` |
| `GET` | `/v1/memory/facts` | `list[MemoryFact]` | `MemoryRegistry().all_facts()` |
| `GET` | `/v1/memory/facts/{fact_id}` | `MemoryFact` | `MemoryRegistry().show(fact_id)` |
| `GET` | `/v1/insights` | `InsightsWindow` | `insights.build_window(since)` |
| `GET` | `/v1/outcomes/summary` | `OutcomesSummary` | `outcome_capture.summary(since)` |
| `GET` | `/v1/outcomes/{session_id}` | `list[OutcomeEntry]` | `outcome_capture.outcomes_for(session_id)` |
| `GET` | `/v1/reports` | `list[ReportMeta]` | reads `reports/index.json` |
| `GET` | `/v1/reports/{week}` | `{md: str, json: dict}` | reads `reports/<week>/benchmark.{md,json}` |

### Frontend (`frontend/src/`)

| File | What changes |
|------|-------------|
| `api.ts` | Add the 9 client functions: `sessions()`, `sessionReport(id)`, `memoryFacts(vendor?)`, `memoryFact(id)`, `insightsWindow(since)`, `outcomesSummary(since)`, `outcomesForSession(id)`, `reports()`, `report(week)`. Add matching TypeScript interfaces. |
| `pages/Sessions.tsx` | **New.** Table view + click-through to detail. |
| `pages/SessionDetail.tsx` | **New.** Renders `SessionReport`. |
| `pages/Memory.tsx` | **Modify.** Add vendor tabs: "Cross-vendor (new)" + "Knowledge blocks (legacy)". Default tab = cross-vendor. |
| `pages/Insights.tsx` | **Rewrite.** Replace telemetry view with `InsightsWindow`. Move telemetry to new `Telemetry.tsx`. |
| `pages/Telemetry.tsx` | **New.** Holds the old Insights telemetry content. |
| `pages/Outcomes.tsx` | **New.** Show route + compact outcome scores. |
| `pages/Reports.tsx` | **New.** List + per-week markdown render. |
| `pages/Traces.tsx` | **Unchanged.** Just remap the route. |
| `App.tsx` | Update NAV_ITEMS, add new Routes, remap `/sessions` to `<Sessions>` (not `<Traces>`), add `/traces` route to `<Traces>`. |
| Tests under `frontend/src/pages/` | Add `Sessions.test.tsx`, `SessionDetail.test.tsx`, `Outcomes.test.tsx`, `Reports.test.tsx`. Update `Memory.test.tsx`, `Insights.test.tsx`. |

## Data model

The TypeScript interfaces mirror the Python dataclasses one-to-one. Backend serializes via `dataclasses.asdict(...)` with `default=str` for datetimes. No new types invented in this spec.

```typescript
// Spec 02
export interface SessionSummary {
  session_id: string;
  started_at: string;
  duration_seconds: number | null;
  vendor: string;
  total_turns: number;
  total_cost_usd: number;
  total_atelier_savings_usd: number;
  label: string | null;
}

export interface SessionReport extends SessionSummary {
  ended_at: string | null;
  models_used: Record<string, number>;
  tool_call_count: number;
  input_token_cost_usd: number;
  cache_write_cost_usd: number;
  cache_read_cost_usd: number;
  output_token_cost_usd: number;
  input_tokens: number;
  cache_write_tokens: number;
  cache_read_tokens: number;
  output_tokens: number;
  routing_downtiered_turns: number;
  routing_savings_usd: number;
  compact_events: number;
  compact_savings_estimate_usd: number;
  top_tools_by_cost: [string, number, number][];
}

// Spec 03
export interface MemoryFact {
  fact_id: string;
  vendor: "claude" | "codex" | "gemini";
  source_path: string;
  source_kind: string;
  content: string;
  line_number: number | null;
  captured_at: string;
  raw_meta: Record<string, unknown>;
}

// Spec 04
export interface InsightsWindow {
  since: string;
  until: string;
  session_count: number;
  total_duration_seconds: number;
  total_cost_usd: number;
  total_atelier_savings_usd: number;
  cost_by_vendor: Record<string, number>;
  cost_by_tool: Record<string, number>;
  cost_by_model: Record<string, number>;
  top_sessions: SessionSummary[];
  outcomes_summary: OutcomesSummary;
  opportunities: Opportunity[];
}

export interface OutcomesSummary {
  route_decisions: number;
  route_avg_score: number;
  compact_events: number;
  compact_avg_score: number;
  sessions_with_high_extra_reads: string[];
}

export interface Opportunity {
  kind: string;
  message: string;
  estimated_savings_usd: number;
  sessions_affected: number;
}

// Spec 05
export interface ReportMeta {
  week: string;              // "2026-W20"
  published_at: string;
  sessions_analysed: number;
  headline_metrics: Record<string, string | number>;
}

export interface ReportContent {
  week: string;
  markdown: string;
  json: Record<string, unknown>;
}
```

## Routing model

Old route → new behaviour:

| Path | Before | After |
|------|--------|-------|
| `/sessions` | renders `Traces` page | renders `Sessions` (cost-report list) |
| `/traces` | doesn't exist | renders `Traces` (the old page) |
| `/insights` | telemetry/cohort view | renders `WeeklyInsights` (spec 04) |
| `/telemetry` | doesn't exist | renders the old telemetry content |
| `/memory` | knowledge blocks only | cross-vendor (default) + knowledge blocks (tab) |
| `/outcomes` | n/a | new |
| `/reports` | n/a | new |

Add legacy redirects so existing bookmarks don't 404. Minimum: `/insights/cohort/*` → `/telemetry`.

## Render rules

- All cost fields formatted USD, two decimals.
- Token counts with thousands separators.
- Vendor names rendered with brand colour: Anthropic = orange, OpenAI = green, Google = blue. Defined as Tailwind constants.
- Tables sortable client-side.
- Empty states use the existing `WorkbenchUI` empty-state component.
- No client-side aggregation — backend returns final shapes.

## Out of scope

- **Editing memory / outcomes.** Read-only, this round.
- **Real-time push.** Polling every 30s is fine.
- **Charts beyond what already exists.** Markdown tables + the existing `SavingsTimeChart` are enough.
- **Mobile-responsive layout.** Desktop-first.
- **Auth changes.** Reuse the existing `verify_api_key` dependency.
- **Spec 06 (cross-machine sync) UI.** Different phase, different spec.

## Acceptance criteria

### Backend
- [x] All 9 new routes registered in `api.py`
- [x] All routes use `Depends(verify_api_key)` 
- [x] All routes return JSON matching the TypeScript interfaces (verified by Pydantic response models or by serializing the dataclass and asserting keys in a unit test)
- [x] `GET /v1/sessions?since=7d` runs in <200ms with 200 sessions
- [x] `GET /v1/memory/facts` runs in <100ms with 1,000 facts
- [x] `GET /v1/insights?since=7d` cached for 60s (idempotent, deterministic for fixed window)
- [x] 404 for unknown session_id, fact_id, week — never 500
- [x] Unit tests in `tests/core/service/test_api_week2_routes.py` cover happy path + 404 for each endpoint
- [x] OpenAPI schema generated (FastAPI default) lists all new routes under correct tags (`sessions`, `memory`, `insights`, `outcomes`, `reports`)

### Frontend
- [x] Every new page renders without console errors
- [x] Sessions table sorts by date desc, click row → SessionDetail
- [x] SessionDetail shows the same numbers as `atelier session report <id>` in the terminal (verified manually)
- [x] Memory page default tab = cross-vendor; legacy knowledge-blocks tab still works
- [x] Insights page shows top sessions, vendor split, opportunities — same as `atelier insights`
- [x] Outcomes page handles empty-state ("no outcomes captured yet")
- [x] Reports page lists `reports/2026-W20/` and renders the markdown content
- [x] All 6 new page-level test files exist (`Sessions`, `SessionDetail`, `Outcomes`, `Reports` new; `Memory`, `Insights` updated)
- [x] Nav order matches the layout in "What — user-visible"
- [x] Old `/insights/*` URLs redirect (not 404) to either `/insights` or `/telemetry`

### Cross-cutting
- [x] `make` / `npm test` passes end-to-end
- [x] No unused imports in `api.ts` (Pyright/ESLint clean)
- [ ] PR description includes a screenshot of each new page

## Open questions for the executor

1. **Memory page merge vs split.** Two valid options: (a) merge old `MemoryBlock` and new `MemoryFact` into one page with vendor tabs (one tab per vendor + a "Knowledge blocks" tab), or (b) split into two pages `/memory/facts` and `/memory/blocks`. **Default: merge (option a)** — fewer nav items, cleaner UX. Decide before PR-2.
2. **Reports markdown rendering.** Pull in a markdown library or render server-side to HTML? **Default: render client-side with `react-markdown`** (already a small dep) — keeps the API endpoint serving raw markdown.
3. **Caching strategy for `/v1/insights`.** 60s cache per-`(since, vendor)` key seems right but adds memory pressure. **Default: 60s LRU cache with max 32 entries, in-memory only.** Revisit if it bites.
4. **Should benchmark reports be authenticated?** They're explicitly meant for public sharing. **Default: still require API key for the JSON endpoint, but the markdown itself is already committed to the repo at `reports/<week>/benchmark.md` — public visibility comes from publishing the markdown, not the API.**
5. **Vendor brand colours.** Anthropic = orange, OpenAI = green, Google = blue is my proposal. Confirm or override in `frontend/src/lib/vendorTheme.ts`.

## Implementation order

Sequence the PRs to land safely:

1. **PR-1: Backend routes.** All 9 endpoints, all tests. No frontend changes. Ship-able alone.
2. **PR-2: API client + types.** Extend `api.ts` only. Verify endpoints with mocked component tests.
3. **PR-3: Sessions + SessionDetail pages + route remap.** Highest-value visible change.
4. **PR-4: Memory page rebuild.** Merge cross-vendor + knowledge blocks. Sensitive — keep legacy working.
5. **PR-5: Insights rebuild + Telemetry split.** Migrate old content to `/telemetry`, build new `WeeklyInsights` at `/insights`.
6. **PR-6: Outcomes + Reports pages.** Lower priority but completes the surface.
7. **PR-7: Legacy redirect cleanup + nav reorder + final QA.**

Each PR ships behind no feature flag — the changes are additive enough that flagging adds complexity without buying safety.

## Dependencies

- Hard: specs 01–05 backend modules (all shipped)
- Hard: existing FastAPI app, existing `verify_api_key` dependency
- Hard: existing `WorkbenchUI` component library
- Soft: existing `SavingsTimeChart` if we add a trend chart to Insights

## Status

- [x] Shipped
- [x] PR-1 (backend routes)
- [x] PR-2 (API client)
- [x] PR-3 (Sessions pages)
- [x] PR-4 (Memory rebuild)
- [x] PR-5 (Insights rebuild + Telemetry split)
- [x] PR-6 (Outcomes + Reports)
- [x] PR-7 (cleanup)
