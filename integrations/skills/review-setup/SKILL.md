---
name: review-setup
argument-hint: [repo-root]
description: "Run the LemonCrow Review loop from the terminal (capture, lc review status --json, feedback, fix) and set up Review surfaces and execution runtimes for any repository."
---

# LemonCrow Review: the loop and the setup

## Run the review loop (agent)

A person reviews in the Review Reader; you, the coding agent, capture your change, read what the reviewer said, fix it, and capture again. The commands are the same in the installed `lc` and in a source checkout (`uv run lc ...`).

1. Capture the change as a new Revision, without opening a browser:

   ```bash
   lc review --no-open --track            # add --repo-root <repo> outside the repository
   ```

   It prints one summary line, `Revision N · F files · T parts · R reviewed`, and `Review  <Reader URL>`. Give the person that URL. Do not run a bare `lc review --no-open` in a source checkout: without `--track` it records nothing.
2. Read the state. It captures nothing:

   ```bash
   lc review status --json
   ```

   Exit code, the same with and without `--json`: `0` ready, `1` not ready yet, `2` no Review found for this repository (capture first), `3` error (server unreachable or a bad command line). The JSON document (`schema: 1`):

   - `state`: `ready`, `not_ready`, `no_review` or `error` (with `message`);
   - `review.ref`, `review.title`, `review.status`; `revision.number` (zero-based) and `revision.label` (`Revision N`, what the person sees);
   - `targets.total`, `targets.reviewed`, `targets.needs_changes`, `targets.changed_since_review`;
   - `comments.unresolved` (open plus orphaned), `feedback.unsent`;
   - `readiness.ready` and `readiness.actions` (for example `Finish review`, `Resolve open comments`); `work_remains`;
   - `risk.text` (`High · <the top fact> · and N more`; levels High, Medium, Low, Not measured), `risk.facts` and `risk.not_measured`; `understanding.text` (`0 of 2 risky parts reviewed`), the same words the Brief shows;
   - `reader_url`.
3. Read the reviewer's feedback as Markdown (it sends nothing anywhere):

   ```bash
   lc review --feedback
   ```

   Act on each open comment, and on every target the person marked `needs_changes`. `lc review --comments` lists the comments with their anchors; `lc review --since-my-review` shows what changed since the person's last review.
4. Fix, run the relevant tests, then go back to step 1. The Review and URL stay the same; what the person already reviewed stays reviewed unless you changed it.
5. Stop when `lc review status` exits `0`, or when the only `readiness.actions` left are the person's (`Finish review`): tell them the Revision and the URL.

Never run `--mark`, `--comment`, `--finish` or `--reopen-review` yourself: they record human judgment, and both the CLI and the server refuse them from a coding-agent session. Never report a Review as approved or ready from anything but `lc review status`. `lc review --help` is one screen; `lc review --help-all` lists every option.

## Set up Review surfaces

Make `lc review` useful for the repository's real product surfaces, not only source diffs. Keep two extension points separate:

- **surface providers** discover *what* a reviewer can inspect (`web`, `bruno`, `service`, custom providers);
- **runner plugins** own *how* an immutable revision executes (`docker-compose`, `command`, or installed runners such as Node/Python/service runners);
- **preview renderers** produce the old/new revision artifacts (image, HTML, text, JSON, download) for a surface whose provider attached a versioned preview descriptor (`storybook`, or installed renderers).

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
5. Keep every runtime grounded in the captured review revision. Working-tree and staged reviews are supported only when LemonCrow can reconstruct the complete immutable revision. Never point Review at today's mutable checkout as a substitute.
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

Review does not run a browser farm. Existing Playwright/browser tests, CI, an external browser agent, or a local hook run the scenario; Review binds the observed result to one exact revision and decides PASS/FAIL only from inspectable checks (completion, assertions, HTTP outcome, console/runtime errors, and a visual threshold only when configured). A reported or model "looks fine" is never PASS.

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

Reporters upload screenshots/traces/recordings with `POST /api/reviews/<id>/evidence/upload?revision_id=<snapshot>&kind=screenshot|playwright_trace|video`, then post one run per revision side to `POST /api/reviews/<id>/browser-checks` with `revision_id`, `scenario`, `side` (`new`, or `old` for the baseline), `viewport`, `environment`, `navigation`, `assertions`, `console`, and `artifacts: {screenshot, trace, video}` evidence ids. A Playwright JSON-reporter test result may be passed as `playwright: {status, errors}`. When nothing applicable is configured or reported, Review shows `NOT_RUN` instead of implying coverage.

## Evidence freshness

Some evidence describes the world at the moment it was observed, not the revision: vulnerability scans, live previews, and typed trusted-security-feed attestations. On an unchanged revision, readiness treats such an observation as `UNKNOWN` once it is older than a max-age, and withdraws an armed landing that relied on it. The built-in max-age is 24 hours. The base branch's `.lemoncrow/review.yaml` may shorten it, but never extend it:

```yaml
evidence_freshness:
  external_observation_max_age: 6h   # seconds, or 90m / 12h / 7d; longer than 24h has no effect
```

`external_observation_max_age` is the only key in this section. If the section is invalid (an unknown key, or a duration that cannot be read), it fails closed: every external observation reads as expired and readiness shows the error. It never falls back to the default. A hosted operator policy can shorten the max-age further (`evidence_max_age_seconds`), and the shortest max-age wins. SBOMs, provenance, and test or CI results stay bound to their revision and do not age this way.

The supply-chain readiness condition is satisfied only by a typed trusted-security-feed attestation. A scan verdict uploaded by a human, or one that only names its source, still blocks on a non-PASS result, but its PASS never satisfies the policy. A reviewer can waive the policy on the latest revision with `POST /api/reviews/<id>/verification/waivers` and `{"revision_id", "requirement_key": "supply-chain-policy", "reason"}`. A current FAIL from the feed itself cannot be waived.

## Guardrails

- Never require a commit merely to obtain Preview; working-tree review is first-class.
- Never copy real secrets into `.lemoncrow/review.yaml`.
- Never use production endpoints/databases as a review environment.
- Never claim an API test executed merely because a localhost service happened to exist.
- Never make a failing runner look successful by silently falling back to a screenshot, live checkout, or unrelated running process.
- Keep generated config small. Auto-detection remains the default; config is an override/escape hatch.
