# LemonCrow Cloud billing workspace

A compact billing and collaboration application used to exercise LemonCrow Review's built-in product surfaces and runtimes.

It contains:

- a React + Vite frontend (`frontend/`) with five routes (`/`, `/pricing`, `/surfaces`, `/team`, `/settings`) for live **Preview / Compare / Source**; `/pricing` loads its plans from `GET /api/plans`;
- a plain-Python HTTP API (`backend/`) with six GET endpoints; `/api/plans` reads `data/plans.csv`;
- Markdown documents with a relative SVG image;
- direct binary media changes (`media/`) for GIF, MP4, WAV, and PDF **Preview / Compare / Source**;
- a Bruno collection (`bruno/`);
- Dockerfiles and `compose.yml`;
- a `docker-compose` Review runtime;
- a sandboxed one-shot `command` Review runtime;
- generic `service` surfaces bound to those runtimes;
- a Storybook component (`frontend/src/components/SurfaceCard.*`) for the built-in `storybook` preview plugin;
- a CSV file (`data/plans.csv`) for the example third-party `csv-table` preview plugin in `demo/review-preview-plugin`.

`npm run dev` proxies `/api` to the API (`API_ORIGIN`, default `http://127.0.0.1:8000`; the Compose frontend uses the `api` service). A static build has no API process, so it serves `frontend/public/api/plans`, the API's answer for the same CSV, in its place; Review's sandboxed visual previews are static builds, and the demo tests keep that file equal to `plans()` on both sides of the change.

The backend API is exercised through the built-in `http-service` Review runtime. It starts the exact frozen revision inside an isolated network namespace, waits for `/health`, runs the API request over loopback, captures the response, and tears the service down. The bounded `command` runtime remains for one-shot checks such as tests.

## Quick start

From the LemonCrow checkout:

```bash
./examples/demo/bootstrap.sh /tmp/lemoncrow-review-demo
uv run lc review --repo-root /tmp/lemoncrow-review-demo --setup
uv run lc review --repo-root /tmp/lemoncrow-review-demo --open
```

Or use `make review-demo` (create and open) and `make review-demo-diff` (show the change). Demo setup installs the CSV plugin into both the checkout and local-server environments and restarts the local server before capture. `make review-demo-plugin` can reinstall it independently.

`bootstrap.sh` creates an independent Git repository, materializes baseline media fixtures, commits the baseline, installs the frontend dependencies (React, Vite, and Storybook), and then applies a prepared uncommitted change across Markdown, frontend, Storybook, CSV, backend, media, Bruno, and Compose files. With the local LemonCrow server running, it creates the Change through the real installed `lc review` path and then seeds plan, code, test-subagent, and review sessions through LemonCrow's real Codex hook/runtime path, including visible agent messages, edit/test ledger evidence, SessionEnd lifecycle, Brief context, and imported Review annotations in that same server-owned Review. For the `standard` scenario it then records the three changed Bruno requests on both sides of Revision 2 (`scripts/record-demo-runs.py`, through the server's runtime-validation route), so the X-Ray (`GET /api/reviews/{id}/xray-graph`) draws those calls as recorded, with their before/after response bodies.

To reset and repeat, run the bootstrap command again.

## Scenarios

Pick one with `bootstrap.sh <dir> --scenario NAME` or `make review-demo SCENARIO=NAME` (default `standard`; the result with no scenario is unchanged). An unknown name fails and lists the valid ones.

| Scenario | State it builds | Status |
| --- | --- | --- |
| `empty` | Baseline committed, no change, no Review captured: the first-run / nothing-to-review state | ready |
| `standard` | The two-phase change described above (docs-only Revision 1, then the full change as Revision 2) | ready |
| `orders` | Baseline gains an orders domain (`backend/orders.py`, `GET /orders/<id>`, tests, `PLAN.md`). One agent change plants three issues: ownerless guest orders open to every signed-in user, the upper discount limit removed, and `order_total` skipping the last item. Only `test_rejects_percent_over_100` catches any of them; the seeded test session does not run it | ready |
| `orders-revision-2` | `orders`, then after Revision 1 is captured a second phase fixes the guest-order rule and `order_total` (each with a new test that fails on Revision 1) and also adds an unrequested `percent = min(percent, 100)` | ready |
| `large` | Deterministic 30-file change on the baseline: backend handlers, frontend components, docs, tests, a generated file, and a lockfile entry | ready |
| `tenant-cache` | Baseline gains invoices for two tenants, Acme and Globex (`backend/invoices.py`): the dashboard, the CSV export and the public API each call `list_invoices()`, and `update_invoice()` edits one. The agent's pull request, committed on `agent/cache-invoice-list` ("Cache the invoice list for 60 seconds", "Adds a 60-second cache. No behavior change."), puts a shared cache keyed `invoices:{status}:{page}` in front of the list, without the tenant, and nothing clears it on update. The Review records three Bruno recipes on base and head: Acme's export, Globex's list after it (Globex is served Acme's invoices), and Acme's dashboard after a correction (still the old amount). The backend tests pass on both sides | ready |
| `busy` | Several Reviews in the queue | TODO, not implemented yet |
| `agent-failed` | The agent session ended in failure | TODO, not implemented yet |
| `github-pr` | A GitHub pull request as the Review source | TODO, not implemented yet |

Scenario definitions live in `scripts/review_scenarios.py`; `scripts/apply-demo-change.py --scenario NAME --phase P` applies them without a server. `orders` agent activity is seeded by `scripts/seed-agent-activity.py --scenario orders` with the task "Let customers view a guest checkout order before they sign in, and let promo codes stack." The server-free checks are in `tests/gateway/test_review_demo_scenarios.py`.

## What to inspect

| Change | Review behavior |
| --- | --- |
| `README.md`, `docs/architecture.md` | Markdown Preview / Compare / Source; the SVG remains textual source |
| `media/demo.gif`, `media/demo.mp4`, `media/demo.wav`, `media/demo.pdf` | Native media Preview / Compare / Source using exact revision-pinned bytes |
| `frontend/src/pages/PricingPage.jsx`, `frontend/src/components/SurfaceCard.*`, `frontend/src/content.json` | Only 3 of the 5 routes are listed (`/pricing`, `/surfaces`, `/`), ranked by visual difference: large, medium, small. `/team` and `/settings` are unchanged and not shown |
| `frontend/src/components/SurfaceCard.jsx`, `SurfaceCard.stories.jsx` | Storybook story screenshots, old vs new; the added `Components` story is reported as missing on the old side |
| `data/plans.csv` | Old/new table preview from the demo-installed `csv-table` plugin. The API serves it, so the List plans response (Team 49 → 59, Enterprise added) and `/pricing` change with it |
| `bruno/ReviewSummary.bru`, `bruno/Plans.bru`, `bruno/Settings.bru` | Only 3 of the 6 requests are listed, each run on both sides against a revision-pinned Python service and ranked by response difference: large, medium, small |
| `compose.yml` | service surfaces backed by the `app-stack` Docker Compose runtime |
| `backend/app.py` | source change plus executable `backend-tests` command surface |

A page or request appears only when the change reaches it: a route is listed when a changed file is imported by that route's page module, and a Bruno request when its `.bru` file changed (or, with a `watch:` glob, when a matching implementation file changed).

The Review configuration lives in `.lemoncrow/review.yaml`. Surface providers answer **what is reviewable**; runtime runners answer **how an immutable revision executes**. They are deliberately independent.

## Preview plugins

The `components` surface uses the built-in Storybook preview plugin. Each side's Storybook is built once inside the sandbox and the changed stories are captured as images, so it needs bubblewrap and Chrome/Chromium like the web preview.

The `tables` surface uses the example third-party plugin. Demo setup installs it automatically into the checkout and the environment that runs the local Review server, then restarts that server. To reinstall independently:

```bash
make review-demo-plugin
uv run lc review --repo-root /tmp/lemoncrow-review-demo --setup
```

`uv sync` removes packages that are not project dependencies, so reinstall the plugin after syncing. See [Review preview plugins](../../docs/reference/review-preview-plugins.md) for the contract and for adding another platform.
