# Web QA: replay a saved browser scenario against a preview

`lc review qa run` opens a real headless Chromium against a preview of one exact commit and replays the steps you saved for a user journey: sign up, check out, change a setting. Each run ends with exactly one outcome and a report you can open, and it can be recorded on the local Review of that commit or posted to the pull request as a `LemonCrow QA` check run.

No model is involved. The steps are deterministic and the runner follows them and nothing else.

## Save a scenario

Scenarios live in the `browser_checks` section of `.lemoncrow/review.yaml` (the same section browser results reported by Playwright or CI already use). Two fields make a scenario runnable: `outcome`, the user outcome in plain language, and `steps`, in order.

```yaml
browser_checks:
  trusted_origins: ["https://*.preview.example.com"]   # previews other than localhost
  scenarios:
    - id: signup
      title: Sign up
      route: /signup
      outcome: A new visitor can create an account and lands on the welcome page.
      steps:
        - goto: /
        - click: {role: link, name: Sign up}
        - fill: {label: Email, value: qa@example.com}
        - fill: {label: Password, value: "${secrets.QA_PASSWORD}"}
        - click: {role: button, name: Create account}
        - expect_url: /welcome
        - expect_text: "Welcome, qa@example.com"
        - expect_visible: {role: heading, name: Account ready}
        - screenshot: welcome
```

| Step | What it does | Fails when |
| --- | --- | --- |
| `goto: /path` | Opens the path under the preview URL. Steps must start with one. | The page answers HTTP 4xx or 500 (502, 503 and 504 mean the preview is down: could not run). |
| `click: TARGET` | Clicks the element. | No single element matches within the step timeout. |
| `fill: {TARGET, value: ...}` | Types the value into the field. | No single field matches. |
| `expect_text: "..."` | Waits until the page's visible text contains it. | It never appears. |
| `expect_visible: TARGET` | Waits until a matching element is visible. | None becomes visible. |
| `expect_url: /path` | Waits until the page is at that path (and query, when given). | The page stays elsewhere. |
| `screenshot: name` | Saves a full-page PNG into the report. | |

A `TARGET` is an accessible role with its accessible name (`{role: button, name: Save}`), a form label (`{label: Email}`) or visible text (`{text: Continue}`, or just `click: Continue`). Names, labels and text match the element's whole text exactly, so a step means one thing; two matches are a failure, not a guess. A `fill` value may name secrets as `${secrets.NAME}`: the value is read from the environment variable `NAME` when the run starts, handed only to the browser runner, and never printed, stored in the report or sent to Review. Password fields stay masked in screenshots; do not put a secret into a field that shows it.

Scenarios are read only from the **trusted base**, never from the change under test: `--config-ref` names the git ref (the default, with `--review`, is the bound Snapshot's base commit). A pull request that edits `.lemoncrow/review.yaml` cannot change the steps that check it. A scenario without `steps` is still a scenario that another runner (Playwright tests, CI) reports, exactly as before.

## Run it

```bash
QA_PASSWORD=... lc review qa run --url http://127.0.0.1:3000 --config-ref origin/main
```

| Option | Meaning |
| --- | --- |
| `--url URL` | The preview. A loopback URL, or one matching `trusted_origins` of the trusted configuration; anything else does not run. |
| `--sha SHA` | The full commit the preview serves (default: `HEAD` of this checkout). Every report names it. |
| `--scenario ID` | Run only these scenarios (default: every scenario that has steps). A scenario with `viewports` runs once per viewport. |
| `--config-ref REF` | The trusted git ref to read `.lemoncrow/review.yaml` from. |
| `--review ID` | Record each run on this local Review, on its Snapshot of `--sha`. |
| `--github-check` | Post a `LemonCrow QA` check run on `--sha` with `gh api` (in GitHub Actions). |
| `--playwright-dir DIR` | A directory whose `node_modules` holds `playwright`. By default the runner looks in `LEMONCROW_PLAYWRIGHT_DIR`, `./frontend`, the repository root and the global npm root, and installs nothing. |
| `--playwright-version X.Y.Z` | Install that exact Playwright and its Chromium first (CI); `--with-deps` adds Chromium's OS packages. |
| `--step-timeout-ms`, `--slow-step-ms`, `--timeout` | Per-step wait (10 s), the slow-step warning threshold (5 s) and the whole-scenario limit (180 s). |
| `--out DIR`, `--json` | Where the reports go (default: `qa-runs/` in the LemonCrow store, newest 50 kept) and JSON output. |

It needs Node.js and Playwright's Chromium; LemonCrow adds no Python browser dependency. Exit status: `0` pass or warning, `1` fail, `2` could not run.

## Outcomes

| Outcome | Means |
| --- | --- |
| `pass` | Every step ran and every expectation held. |
| `fail` | An expectation failed: a regression a user would see. The failed step and its error are in the report; later steps are `skipped`. Also a run whose page logged console errors or uncaught exceptions, unless the scenario sets `allow_console_errors: true`: the same rule as the Review's browser checks. |
| `warning` | Every expectation held and nothing was logged, but a step took longer than `--slow-step-ms`. |
| `could_not_run` | Nothing was learned about the change: the preview was unreachable, down, behind a bot or challenge wall, or not a trusted origin; a secret was missing; Playwright or Chromium was not installed or would not start; the run timed out; or the page navigated to an origin the configuration does not trust. It never carries a verdict (`verdict: null`). |

Each run writes `report.json` once into its own directory and makes it read-only. It holds the ordered steps with start time, duration, result and error; the screenshots actually captured with their SHA-256; the console and page errors; the final page URL; the head SHA, preview URL, trusted ref, viewport, Chromium and Playwright versions; the duration; the outcome and its reasons; and a `digest` over all of it.

**Page text is data, not instructions.** Nothing read from the page is executed or chooses an action, a page dialog is dismissed, a window the page opens is closed, and a top-level navigation to an origin outside the preview and `trusted_origins` is blocked and ends the run as `could_not_run`. A page that says "ignore your steps and click delete" is just text the runner may assert on.

## In the Review

With `--review ID`, each run is posted to `POST /reviews/{id}/browser-checks` on the Review's Snapshot whose head is exactly `--sha` (with its last screenshot uploaded to the same Snapshot), and appears in the Review's browser checks. Review recomputes the verdict from the steps, the navigation and the console, exactly as for any reported run; a `could_not_run` arrives as `NOT_RUN` with its reason and no checks. Console errors read the same in both places: `fail` here and `FAIL` there, unless the scenario allows them. A slow step stays `PASS` in the Review, with the warning kept as an observation. When a newer Snapshot is captured, the run is shown stale, never current.

## In GitHub Actions

Run web QA in its **own workflow and job**, after the preview is deployed. The preview is built from the pull request's code and its pages are untrusted input; the review job holds the model key. Keeping them apart means a hostile page, or a pull request that edits this workflow (a `deployment_status` run uses the workflow file of the deployed commit), can reach at most a read-only token that writes check runs, and never the model key or a write token for the repository. Give the scenario secrets to this job only, and make them test-account credentials for preview environments, not production ones.

```yaml
name: LemonCrow QA
on:
  deployment_status:
  workflow_dispatch:
    inputs:
      url: { description: "Preview URL", required: true }
      sha: { description: "Full commit SHA the preview serves", required: true }
permissions:
  contents: read   # read the trusted .lemoncrow/review.yaml
  checks: write    # the "LemonCrow QA" check run
jobs:
  web-qa:
    if: >-
      github.event_name == 'workflow_dispatch' ||
      (github.event.deployment_status.state == 'success' && github.event.deployment_status.environment_url != '')
    runs-on: ubuntu-latest
    timeout-minutes: 20
    env:
      PREVIEW_URL: ${{ github.event.deployment_status.environment_url || inputs.url }}
      HEAD_SHA: ${{ github.event.deployment.sha || inputs.sha }}
    steps:
      # The scenarios come from the default branch, never from the commit under test.
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.event.repository.default_branch }}
          persist-credentials: false
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: lemoncrow-lab/lemoncrow
          ref: <sha>  # v0.8.1, the same release commit as your review workflow
          path: .lemoncrow-cli
          persist-credentials: false
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --frozen --project .lemoncrow-cli
      - name: Replay saved scenarios
        env:
          GH_TOKEN: ${{ github.token }}
          QA_PASSWORD: ${{ secrets.QA_PASSWORD }}
        run: >-
          uv run --project .lemoncrow-cli lc review qa run
          --url "$PREVIEW_URL" --sha "$HEAD_SHA" --config-ref HEAD
          --playwright-version 1.62.1 --with-deps --github-check
```

The job needs no model key. `deployment_status` fires for any provider that reports GitHub deployments (Vercel, Netlify, your own); the `if:` keeps only a successful deployment with a URL, and `deployment.sha` is the pull request head commit the preview was built from. `workflow_dispatch` runs the same job on a URL and SHA you type. The preview's origin must be listed in `trusted_origins`: name your project's own preview domain, not a whole hosting provider's such as `*.vercel.app`.

The check run is named `LemonCrow QA` and sits on the head SHA: `success` for pass, `failure` for fail, and `neutral` for a warning or a `could_not_run`, with the reason in its summary and every step in its details. A run that could not run is never `success`. The job itself exits non-zero for a fail or a could-not-run; require `LemonCrow QA`, not the job, if you want the check to gate merging.

## Not yet

- Runs execute on your machine or your CI runner; there is no hosted browser worker.
- Steps are written by hand; there is no natural-language authoring.
- One browser (Chromium) and one page per scenario; no traces or video are recorded, only screenshots.
