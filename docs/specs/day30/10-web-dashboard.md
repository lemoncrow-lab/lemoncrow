# Spec 10 — Web Dashboard MVP

> Phase 2. Conversion driver to Pro tier.

## Why

Terminal output is great for daily use. A web dashboard is what makes a developer share Atelier with their CTO. It's also where Pro-tier features (sync, history beyond 30 days, charts) become visible.

This spec is a thin MVP — read-only, no editing, no team features.

## What — user-visible

URL: `https://atelier.dev/dashboard` (production) or `http://localhost:8765` (local mode)

**Pages:**

**Overview** — weekly spend, cost-by-vendor pie, top sessions list

**Sessions** — sortable table, click for `SessionReport` view

**Memory** — facts from all vendors, grouped, searchable

**Outcomes** — route + compact outcome scores over time

**Settings** — sync status, machines, API key presence (not values)

**Local mode**: serves from local data, single-user, no auth needed.

**Cloud mode** (Pro tier): requires login, reads synced data.

## Where — files

This is a separate frontend repo: **`atelier-dashboard`**. Out of this monorepo's scope to build the React/Next.js app itself. This spec describes the **Atelier-side API** the dashboard reads from.

| File                                    | What changes (in this repo)                |
| --------------------------------------- | ------------------------------------------ |
| `src/atelier/gateway/web/__init__.py` | **New package.**                     |
| `src/atelier/gateway/web/server.py`   | Local HTTP server (FastAPI)                |
| `src/atelier/gateway/web/routes.py`   | Read-only JSON endpoints                   |
| `src/atelier/gateway/adapters/cli.py` | Add `web` command: `atelier web start` |

## Local web server

```bash
$ atelier web start
Atelier dashboard running at http://localhost:8765
Press Ctrl-C to stop.

$ atelier web start --port 9000
$ atelier web start --background
```

The server binds **only to 127.0.0.1** (never 0.0.0.0). No auth needed in local mode because it's localhost-only.

## API endpoints

```
GET /api/insights?since=7d            → InsightsWindow JSON (spec 04)
GET /api/sessions                     → [SessionSummary] list
GET /api/sessions/<id>                → SessionReport (spec 02)
GET /api/sessions/<id>/counterfactual → counterfactual data (spec 07)
GET /api/memory                       → all memory facts
GET /api/memory/<fact-id>             → single fact
GET /api/outcomes                     → outcome data (spec 01)
GET /api/sync/status                  → sync status (spec 06)
```

All endpoints return JSON. No mutations in MVP.

## Out of scope

- **Editing memory or settings.** Read-only.
- **Real-time push** (WebSockets). Polling every 30s is fine for MVP.
- **Team / multi-user view.** Spec 12.
- **Mobile responsive.** Desktop-first.
- **Cloud-hosted version.** Local-only MVP; cloud-hosted is Pro-tier feature.
- **Authentication.** Not needed for localhost-only MVP.

## Acceptance criteria

- [ ] `atelier web start` opens browser to dashboard
- [ ] All five pages render with real data
- [ ] No internet required for local mode
- [ ] Server binds to 127.0.0.1 only (verified by test)
- [ ] API endpoints return correct JSON shapes matching specs 02, 04, 07
- [ ] Server gracefully shuts down on Ctrl-C
- [ ] Page load <500ms on 100-session dataset

## Open questions

1. **Frontend stack for atelier-dashboard repo.** Recommendation: Next.js + Tailwind + TanStack Query. Decision out of this spec.
2. **Hosted version timing.** When do we deploy `atelier.dev/dashboard` with cloud auth? **Default: after spec 06 (sync) ships — needs the cloud backend anyway.**
3. **Charts library.** Recharts or Tremor? **Defer to dashboard repo PR.**

## Status

- [ ] Pending
- [ ] In progress
- [ ] Shipped
