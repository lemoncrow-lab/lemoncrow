---
name: review-setup
argument-hint: [repo-root]
description: "Set up LemonCrow Review surfaces and self-contained execution runtimes for any repository."
---

# LemonCrow Review setup

Make `lc review` useful for the repository's real product surfaces, not only source diffs. Keep two extension points separate:

- **surface providers** discover *what* a reviewer can inspect (`web`, `bruno`, `service`, custom providers);
- **runner plugins** own *how* an immutable revision executes (`docker-compose`, `command`, or installed runners such as Node/Python/service runners);
- **preview renderers** produce the old/new Snapshot artifacts (image, HTML, text, JSON, download) for a surface whose provider attached a versioned preview descriptor (`storybook`, or installed renderers).

A surface may bind to a runtime by ID. Never put Docker/npm/Python execution logic inside a surface provider.

## Workflow

1. Resolve the repository root from the current workspace or the path the user gave you. Never configure a sibling repo by guesswork.
2. Inspect first:

   ```bash
   lc review --repo-root <repo> --setup --json
   ```

   Treat detected surfaces and detected runtimes as separate facts. Do not invent framework names, compose files, Bruno collections, commands, routes, or ports.
3. If there is no `.lemoncrow/review.yaml` and useful candidates are marked `auto_write: true`, write the deterministic baseline:

   ```bash
   lc review --repo-root <repo> --setup --write-review-config
   ```

   If a config already exists, edit it deliberately; never overwrite it.
4. Review `.lemoncrow/review.yaml`. The stable shape is:

   ```yaml
   version: 1

   runtimes:
     - id: app-stack
       runner: docker-compose
       compose:
         - compose.dev.yml
       services: [app, api]

     - id: checks
       runner: command
       root: backend
       command: [uv, run, pytest, -q]
       timeout: 120

   surfaces:
     - id: storefront
       provider: web
       root: frontend
       framework: next
       routes: auto

     - id: api-contract
       provider: bruno
       collection: backend/bruno
       environment: development
       # Bind only when this runtime actually starts/provides the reviewed API.
       # runtime: app-stack

     - id: services
       provider: service
       runtime: app-stack
       services: [app, api]

     - id: components
       provider: storybook
       root: frontend        # directory containing .storybook/
       preview:
         timeout: 180        # seconds; host cap is 300
   ```

   Built-in surface providers are `web`, `bruno`, `service`, and `storybook`. Built-in runners currently include `docker-compose`, bounded `command`, and `http-service`. Installed extensions can add providers through `lemoncrow.review_surface_providers`, runners through `lemoncrow.review_runners`, and preview renderers through `lemoncrow.review_preview_renderers` (see `docs/reference/review-preview-plugins.md`).
5. Keep every runtime grounded in the captured review revision. Working-tree and staged reviews are supported only when LemonCrow can reconstruct the complete immutable snapshot. Never point Review at today's mutable checkout as a substitute.
6. Re-run setup validation after edits:

   ```bash
   lc review --repo-root <repo> --setup
   ```

   Resolve every `error:`. `info:` lines are recommendations, not failures.
7. Dogfood the result on an actual change:

   ```bash
   lc review --repo-root <repo> --open
   ```

   For UI changes, confirm `Preview | Compare | Source` and affected route selection. For executable surfaces, confirm the surface names its runtime and the result names the runner that executed it. For plugin previews (Storybook stories, installed renderers), confirm both sides render or name a specific reason, and that `--setup` reports no plugin `error:` lines.

## Choosing a runner

- **Docker Compose:** use `runner: docker-compose`; keep compose files/services in the runtime, not the surface. The same runtime can back web, service, Bruno, or custom surfaces.
- **One-shot Python/Node/Make checks:** use `runner: command` with an argv list. Do not use shell strings. This runner is bounded and is not a long-lived dev-server runner.
- **Long-lived `npm run dev`, ASGI/Flask, JVM, etc.:** use/install a service runner that owns start, health, endpoint publication, and teardown as one self-contained plugin. Do not fake this with a one-shot command runner.
- **Something custom:** prefer a `ReviewRunner` plugin when the difference is execution technology; prefer a `ReviewSurfaceProvider` when the difference is what the human reviews. Many integrations need one of each, not a combined mega-plugin.

## Examples

A Bruno collection does not care how the API starts:

```yaml
runtimes:
  - id: api
    runner: python-service
    root: backend
    command: [uv, run, uvicorn, app.main:app]
    health: /healthz

surfaces:
  - id: api-tests
    provider: bruno
    collection: backend/bruno
    runtime: api
```

The same surface can instead bind to Docker:

```yaml
runtimes:
  - id: api
    runner: docker-compose
    compose: [compose.dev.yml]
    services: [api]

surfaces:
  - id: api-tests
    provider: bruno
    collection: backend/bruno
    runtime: api
```

## Browser regression evidence

Review does not run a browser farm. Existing Playwright/browser tests, CI, an external browser agent, or a local hook run the scenario; Review binds the observed result to one exact Snapshot and decides PASS/FAIL only from inspectable checks (completion, assertions, HTTP outcome, console/runtime errors, and a visual threshold only when configured). A reported or model "looks fine" is never PASS.

Declare scenarios in the base branch's `.lemoncrow/review.yaml` (the base governs, so a Change cannot weaken the checks gating it):

```yaml
browser_checks:
  trusted_origins: ["https://*.preview.example.com"]   # loopback is always trusted
  scenarios:
    - id: checkout
      title: Checkout regression
      route: /checkout
      paths: ["web/src/checkout/*"]   # or omit and match the Web route the Change exposes
      required: true                    # optional scenarios inform but never block landing
      viewports: ["1280x800"]
      visual_threshold: 0.01            # only counts when configured here
```

Reporters upload screenshots/traces/recordings with `POST /api/reviews/<id>/evidence/upload?revision_id=<snapshot>&kind=screenshot|playwright_trace|video`, then post one run per Snapshot side to `POST /api/reviews/<id>/browser-checks` with `revision_id`, `scenario`, `side` (`new`, or `old` for the baseline), `viewport`, `environment`, `navigation`, `assertions`, `console`, and `artifacts: {screenshot, trace, video}` evidence ids. A Playwright JSON-reporter test result may be passed as `playwright: {status, errors}`. When nothing applicable is configured or reported, Review shows `NOT_RUN` instead of implying coverage.

## Guardrails

- Never require a commit merely to obtain Preview; working-tree review is first-class.
- Never copy real secrets into `.lemoncrow/review.yaml`.
- Never use production endpoints/databases as a review environment.
- Never claim an API test executed merely because a localhost service happened to exist.
- Never make a failing runner look successful by silently falling back to a screenshot, live checkout, or unrelated running process.
- Keep generated config small. Auto-detection remains the default; config is an override/escape hatch.
