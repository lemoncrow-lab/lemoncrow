# LemonCrow Review demo

A deliberately small application that exercises LemonCrow Review's built-in review surfaces and runtimes.

It contains:

- a React + Vite frontend (`frontend/`) with five routes (`/`, `/pricing`, `/surfaces`, `/team`, `/settings`) for live **Preview / Compare / Source**;
- a plain-Python HTTP API (`backend/`) with six GET endpoints;
- Markdown documents with a relative SVG image;
- direct binary media changes (`media/`) for GIF, MP4, WAV, and PDF **Preview / Compare / Source**;
- a Bruno collection (`bruno/`);
- Dockerfiles and `compose.yml`;
- a `docker-compose` Review runtime;
- a sandboxed one-shot `command` Review runtime;
- generic `service` surfaces bound to those runtimes;
- a Storybook component (`frontend/src/components/SurfaceCard.*`) for the built-in `storybook` preview plugin;
- a CSV file (`data/plans.csv`) for the example third-party `csv-table` preview plugin in `demo/review-preview-plugin`.

The backend API is exercised through the built-in `http-service` Review runtime. It starts the exact frozen revision inside an isolated network namespace, waits for `/health`, runs the API request over loopback, captures the response, and tears the service down. The bounded `command` runtime remains for one-shot checks such as tests.

## Quick start

From the LemonCrow checkout:

```bash
./demo/review-surfaces/bootstrap.sh /tmp/lemoncrow-review-demo
uv run lc review --repo-root /tmp/lemoncrow-review-demo --setup
uv run lc review --repo-root /tmp/lemoncrow-review-demo --open
```

Or use `make review-demo` (create and open), `make review-demo-diff` (show the change), and `make review-demo-plugin` (install the CSV plugin) from the checkout root.

`bootstrap.sh` creates an independent Git repository, materializes baseline media fixtures, commits the baseline, installs the frontend dependencies (React, Vite, and Storybook), and then applies a prepared uncommitted change across Markdown, frontend, Storybook, CSV, backend, media, Bruno, and Compose files.

To reset and repeat, run the bootstrap command again.

## What to inspect

| Change | Review behavior |
| --- | --- |
| `README.md`, `docs/architecture.md` | Markdown Preview / Compare / Source; the SVG remains textual source |
| `media/demo.gif`, `media/demo.mp4`, `media/demo.wav`, `media/demo.pdf` | Native media Preview / Compare / Source using exact revision-pinned bytes |
| `frontend/src/pages/PricingPage.jsx`, `frontend/src/components/SurfaceCard.*`, `frontend/src/content.json` | Only 3 of the 5 routes are listed (`/pricing`, `/surfaces`, `/`), ranked by visual difference: large, medium, small. `/team` and `/settings` are unchanged and not shown |
| `frontend/src/components/SurfaceCard.jsx`, `SurfaceCard.stories.jsx` | Storybook story screenshots, old vs new; the added `Components` story is reported as missing on the old side |
| `data/plans.csv` | Old/new table preview from the `csv-table` plugin once installed; source only until then |
| `bruno/ReviewSummary.bru`, `bruno/Plans.bru`, `bruno/Settings.bru` | Only 3 of the 6 requests are listed, each run on both sides against a revision-pinned Python service and ranked by response difference: large, medium, small |
| `compose.yml` | service surfaces backed by the `app-stack` Docker Compose runtime |
| `backend/app.py` | source change plus executable `backend-tests` command surface |

A page or request appears only when the change reaches it: a route is listed when a changed file is imported by that route's page module, and a Bruno request when its `.bru` file changed (or, with a `watch:` glob, when a matching implementation file changed).

The Review configuration lives in `.lemoncrow/review.yaml`. Surface providers answer **what is reviewable**; runtime runners answer **how an immutable revision executes**. They are deliberately independent.

## Preview plugins

The `components` surface uses the built-in Storybook preview plugin. Each side's Storybook is built once inside the sandbox and the changed stories are captured as images, so it needs bubblewrap and Chrome/Chromium like the web preview.

The `tables` surface uses the example third-party plugin. Until it is installed, `--setup` reports `surface 'tables' uses unavailable provider 'csv-table'` and `data/plans.csv` stays reviewable as source. Install it into the environment that runs the Review server:

```bash
uv pip install -e ./demo/review-preview-plugin
bash scripts/local_server.sh restart   # only if the local server was already running
uv run lc review --repo-root /tmp/lemoncrow-review-demo --setup
```

`uv sync` removes packages that are not project dependencies, so reinstall the plugin after syncing. See [Review preview plugins](../../docs/reference/review-preview-plugins.md) for the contract and for adding another platform.
