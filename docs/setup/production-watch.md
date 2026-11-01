# Production watch: what production saw after a deploy

`lc review watch-deploy` takes one deployed commit SHA and a window after the deploy (2 hours by default), finds the pull request that introduced the SHA, and asks your own telemetry what happened in that window: Sentry for the issues first seen in that release, Prometheus for one error-rate query before versus after the deploy. It prints a Markdown report (or `--json`), and with `--comment` writes it as one comment on that pull request, updated in place on every rerun.

It only reads. Sentry and Prometheus get GET requests and nothing else; no issue is created, resolved or assigned, no alert is touched, and nothing is fixed or rolled back. The telemetry credential is sent only to the base URL you configure, never to LemonCrow, and no output prints it. No model is involved.

## Outcomes

Every run ends with exactly one outcome.

| Outcome | Means |
| --- | --- |
| `signal` | At least one Sentry issue first seen in this release has a stack frame in a file the pull request changed (**linked**), or a Prometheus series rose by more than the threshold you configured. |
| `no_new_signal` | Every configured connector answered and nothing was linked or crossed. The report says "Nothing new observed in 2h" (or how many new issues there were, none in the pull request's files). It never says "healthy": a quiet window is not proof. |
| `not_observed` | Nothing can be said: no connector is configured, a credential is missing or refused, an API answered with an error, Prometheus returned no data, Sentry does not know the release name, or the window has not elapsed yet. The report names the reason. It is never a verdict. |

When a connector answered and another did not, the outcome is `not_observed` unless the one that answered found a signal; the report still shows what the answering connector saw.

**Linking.** The pull request comes from `GET /repos/{owner}/{repo}/commits/{sha}/pulls` (the merged pull request whose merge commit is the SHA, else a merged one, else the first listed) and its changed files from `GET /repos/{owner}/{repo}/pulls/{n}/files`, both through `gh api`. A Sentry issue is linked only when a frame of its latest event names one of those files: the same path, a deployed absolute path that ends with it (`/srv/app/src/api/users.py` for `src/api/users.py`), or a path relative to a source root that it ends with (`api/users.py`). Frames under `site-packages/`, `dist-packages/` or `node_modules/` never link. Every other new issue is listed as "in this release, not linked to its files". A SHA no pull request contains (a direct push, a revert pushed without a pull request, an unrelated SHA) is said plainly, and then nothing is linked and there is no pull request to comment on.

## Run it

```bash
export SENTRY_AUTH_TOKEN=...   # read scopes: project:read, event:read, org:read
lc review watch-deploy --sha 3f9c0a1... --deployed-at 2026-10-31T09:00:00Z \
  --sentry-org acme --sentry-project web
```

| Option | Meaning |
| --- | --- |
| `--sha SHA` | The deployed commit (required). |
| `--deployed-at TIME` | When it reached production, ISO 8601 with a zone (default: now, so the window has not elapsed). |
| `--window 2h` | How long after the deploy to look, `5m` to `24h`. The run queries nothing until the window has closed. |
| `--repo owner/name` | Default `$GITHUB_REPOSITORY`, else the checkout `gh` is in. |
| `--sentry-url`, `--sentry-org`, `--sentry-project` | Or `$SENTRY_URL` (default `https://sentry.io`), `$SENTRY_ORG`, `$SENTRY_PROJECT`. Sentry is used when an organization or project is given. |
| `--sentry-token-env NAME` | The variable that holds the token (default `SENTRY_AUTH_TOKEN`). A Sentry organization token made for uploads (`org:ci`) cannot read issues; use an internal integration or a personal token with read scopes. |
| `--sentry-release TEMPLATE` | The release name your app reports, with `{sha}` and `{short_sha}` (default `{sha}`); for example `web@{short_sha}`. A release Sentry does not know is `not_observed`, not "nothing new". |
| `--sentry-environment NAME` | Only issues in this Sentry environment (or `$SENTRY_ENVIRONMENT`). |
| `--prom-url`, `--prom-query` | Or `$PROMETHEUS_URL`, `$PROMETHEUS_QUERY`. Both are needed. |
| `--prom-token-env NAME` | A variable holding a bearer token for Prometheus, when it needs one. Named and empty is a missing credential. |
| `--prom-max-increase X` | The rise (after minus before) that counts as a signal. Without it the numbers are reported and never judged. |
| `--route GLOB=ROUTE` | Repeatable: a changed file matching `GLOB` touches `ROUTE`, for example `--route 'src/api/users/*=/api/users'`. |
| `--json` | The machine-readable report (the outcome is `.outcome`). |
| `--comment` | Write the report on the pull request that introduced the SHA, and update that same comment on reruns. |
| `--no-record` | Do not append the result to a local Review (see below). |

Base URLs must be `https` (plain `http` only on a loopback address), without credentials, query or fragment. The exit status is `0` for every outcome, so a workflow does not go red because production was quiet or unreachable; it is `1` when `--comment` was asked and the comment could not be written.

**Prometheus.** `--prom-query` is one error-rate expression. `{route}` in it is replaced by the route as a regular expression, so match it with `=~`:

```promql
sum(rate(http_requests_total{status=~"5..",route=~"{route}"}[5m]))
  / sum(rate(http_requests_total{route=~"{route}"}[5m]))
```

The run evaluates `avg_over_time((QUERY)[W:])` at the deploy time (the window before) and at the end of the window (the window after), so both sides cover the same length. With `--route` mappings it reports each route the pull request touched (up to 10); without them, or when it touched none, the overall series with `{route}` as `.*`.

**The local Review.** When the LemonCrow store on this machine (`$LEMONCROW_ROOT`, the one `lc review` writes) holds a Review of that pull request, or of the exact SHA, the result is appended to it as deployment evidence with freshness class `external_observation`, on the Snapshot of the SHA or else the Review's current one. It is immutable and outside readiness; a rerun with the same outcome adds nothing. A CI runner normally has no such store, so in CI the comment is the record.

## In CI

The workflow below needs no model key. On a successful production deployment it posts the comment right away (`not_observed` until the window closes, saying when), and an hourly run reruns the watch for the recent production deployments whose window has closed, which updates that same comment with what production saw. `workflow_dispatch` watches a SHA you type.

```yaml
# .github/workflows/lemoncrow-production-watch.yml
name: LemonCrow production watch
on:
  deployment_status:
  schedule:
    - cron: "23 * * * *"
  workflow_dispatch:
    inputs:
      sha: {description: "Deployed commit SHA", required: true}
      deployed_at: {description: "Deploy time, ISO 8601 with a zone", required: true}
permissions:
  contents: read
  deployments: read      # list the production deployments (scheduled run)
  pull-requests: write   # the one comment on the pull request
jobs:
  watch:
    if: >-
      github.event_name != 'deployment_status' ||
      (github.event.deployment_status.state == 'success' &&
       github.event.deployment_status.environment == 'production')
    runs-on: ubuntu-latest
    timeout-minutes: 15
    env:
      GH_TOKEN: ${{ github.token }}
      SENTRY_ORG: acme
      SENTRY_PROJECT: web
      SENTRY_AUTH_TOKEN: ${{ secrets.SENTRY_AUTH_TOKEN }}   # the only secret
      WINDOW: 2h
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: lemoncrow-lab/lemoncrow
          ref: <sha>  # v0.8.1, the same release commit as your review workflow
          path: .lemoncrow-cli
          persist-credentials: false
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --frozen --project .lemoncrow-cli
      - name: Watch
        env:
          EVENT_SHA: ${{ github.event.deployment.sha || inputs.sha }}
          EVENT_AT: ${{ github.event.deployment_status.created_at || inputs.deployed_at }}
        run: |
          watch() {
            uv run --project .lemoncrow-cli lc review watch-deploy \
              --sha "$1" --deployed-at "$2" --window "$WINDOW" --comment
          }
          if [ "$GITHUB_EVENT_NAME" != schedule ]; then
            watch "$EVENT_SHA" "$EVENT_AT"
            exit 0
          fi
          # The last 5 production deployments that succeeded and whose window closed in the last 26 hours.
          gh api "repos/$GITHUB_REPOSITORY/deployments?environment=production&per_page=5" --jq '.[] | "\(.id) \(.sha)"' |
          while read -r id sha; do
            at=$(gh api "repos/$GITHUB_REPOSITORY/deployments/$id/statuses?per_page=1" \
              --jq '.[] | select(.state == "success") | .created_at')
            [ -n "$at" ] || continue
            age=$(( $(date -u +%s) - $(date -u -d "$at" +%s) ))
            if [ "$age" -ge 7200 ] && [ "$age" -le 93600 ]; then watch "$sha" "$at" || true; fi
          done
```

The `7200` in the age test is the window in seconds; change it with `WINDOW`. `deployment_status` fires for any deploy tool that reports GitHub deployments (use your tool's name for the production environment in the `if:` and the scheduled listing); `deployment.sha` is the commit that was deployed, normally the merge commit of the pull request. The token is the workflow's own `GITHUB_TOKEN`, so the comment is written by `github-actions[bot]`; a rerun adopts only a comment that carries its hidden marker (`<!-- lemoncrow:watch-deploy sha=... -->`) and was written by a bot or by the signed-in user, so a person quoting the marker is never edited. For Prometheus add `PROMETHEUS_URL` and `PROMETHEUS_QUERY` to `env:` (plus `--prom-token-env` and its secret if it needs one) and `--prom-max-increase` to the command when you want a threshold.

## Not yet

- One page of Sentry issues: the 25 most frequent new issues of the release, each linked from its latest event only. A release with more says "there may be more".
- One Prometheus expression; no per-route latency, alerts or traces here (the hosted customer-worker observation covers those, see [the Review reference](../reference/review.md#production-observation-on-a-customer-worker)).
- No issue, task or fix is created from a signal; the comment is advisory and a person decides.
