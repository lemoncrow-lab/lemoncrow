# GitHub Action: AI pull request review

LemonCrow reviews each pull request in your own CI, on your own model key, and posts the result as a bot: one summary comment with a verdict, inline comments on the lines that matter, and a `LemonCrow Review` commit status on the head commit. On each push it re-reviews only what changed. Early access.

Nothing is sent to LemonCrow. The Action runs `lc review --pr N --ai --publish-bot` on your runner and talks only to GitHub and to the model provider you give it a key for. You pay your provider directly; LemonCrow adds no markup.

## Install the GitHub Action

1. Add your model key as a repository secret: `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.
2. Add this workflow as `.github/workflows/lemoncrow-review.yml`:

```yaml
name: LemonCrow Review

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  pull-requests: write
  statuses: write

concurrency:
  group: lemoncrow-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: lemoncrow-lab/lemoncrow@v1
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          # or: OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        with:
          fail-on: p0
```

3. Open a pull request. The bot posts a summary comment and a `LemonCrow Review` status when the run finishes.

Notes on the workflow:

- **`permissions`** are the least the Action needs: `pull-requests: write` to comment and resolve threads, `statuses: write` for the status, `contents: read` to fetch the pull request. It posts as `github-actions[bot]` with the workflow's `GITHUB_TOKEN`; keep the default `github-token` input so the comments never come from a person.
- **`concurrency`** per pull request with `cancel-in-progress` means a newer push cancels the review of an older one instead of queueing behind it.
- **`fetch-depth: 0`** gives LemonCrow the history to find the merge base. `ready_for_review` is in `types` so a draft is reviewed when it is marked ready.
- **`v1`** is the major tag cut at release. Before it exists, or to pin exactly what you tested, use a full commit SHA: `lemoncrow-lab/lemoncrow@<sha>`. The Action installs LemonCrow from its own checkout at that ref, so the version you pin is the version that reviews.
- Runners: Linux and macOS (the install is POSIX shell); Windows runners are not supported. The key must reach the step as an environment variable; the Action never prints it.

### Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `github-token` | `${{ github.token }}` | Token the bot comments with. |
| `model-provider` | `auto` | `anthropic`, `openai` or `litellm`. `auto` uses `ANTHROPIC_API_KEY`, else `OPENAI_API_KEY`, from the step's environment. |
| `model` | the provider's own | Model id (also `LEMONCROW_REVIEW_MODEL`). OpenAI defaults to `gpt-4o-mini`; Anthropic to `claude-sonnet-4-6`. Both are provisional until review cost per catch is measured. |
| `depth` | `auto` | `auto`, `thorough` or `deep`. `auto` follows `review.effort` in `.lemoncrow/review.yaml` (Auto by default, `quick` if you set it there); the others go deeper for the run. |
| `fail-on` | `none` | `p0` fails the `LemonCrow Review` status while a P0 finding is open. |
| `review-drafts` | `false` | Review draft pull requests too. |
| `on-push` | `true` | Re-review on each push to an open pull request. `false` reviews only when it opens. |
| `pr-number` | the event's pull request | Set it for `workflow_dispatch` runs. |
| `working-directory` | `.` | Where the repository is checked out. |

LiteLLM users can set `model-provider: litellm` and the variables LiteLLM already reads for their cloud (`AWS_*`, `AZURE_*`, `VERTEXAI_*`) plus `LEMONCROW_LITELLM_MODEL`. `LEMONCROW_LLM_BACKEND` is respected if you set it yourself. The intelligence policy and its kill switch (`intelligence.enabled` in the LemonCrow settings, `LEMONCROW_ROOT` on a self-hosted runner) apply exactly as they do locally: when AI is off, the run exits 0 and posts nothing.

## What the bot posts

**One summary comment**, found and updated in place by a hidden marker (`<!-- lemoncrow:summary v1 -->`), so a push edits it instead of adding another:

- a verdict line that agrees with the status: `1 must-fix before merge`, or `No must-fix findings`;
- a status strip: `Re-reviewed 38 changed lines · 1 new · 2 carried forward · 1 fixed since last review`;
- findings sorted by severity, each linking to its inline thread. **P0** must fix, **P1** should fix, **P2** optional;
- P2 findings in a collapsed block, `N low-priority findings held back`;
- on a clean review, a collapsed `What we checked`: files, rules applied, depth;
- a collapsed `Fix with your agent`: a prompt listing the open findings with `file:line`, to paste into Claude Code, Codex or Cursor;
- a footer with the model, depth, model calls and tokens, and an estimated cost when LemonCrow's local price table knows the model. It never guesses a price: an unknown model shows tokens only.

**Inline comments** for P0 and P1 findings: a title with the badge, why, the consequence, and a GitHub `suggestion` block only when the finding carries replacement text. P2 findings stay in the summary.

A hidden JSON block closes the summary (see below), and every inline comment carries `<!-- lemoncrow:finding <id> -->`.

Findings are AI output, so they are never posted as an approval or a change request: the bot's reviews use event `COMMENT` only, and human review state stays human. Anything the model wrote is neutralised before it is posted: it cannot forge the markers above, mention people, load images or open HTML blocks.

### Severity

Severity comes from the finding's validated category, never from the model's wording. **P0** is a violation of a rule you wrote in `.lemoncrow/review.yaml`; correctness, security, API, concurrency and persistence findings are **P1**; performance and verification findings are **P2**. That makes `fail-on: p0` a gate on your own rules.

### The check

The `LemonCrow Review` commit status is set on the reviewed head, with a link to the summary comment:

| Situation | Status |
| --- | --- |
| No P0 finding | `success` |
| A P0 finding and `fail-on: p0` | `failure` |
| A P0 finding and `fail-on: none` | `success`, described as advisory |
| Review incomplete (a model call failed) | `error` with `fail-on: p0`, otherwise `success` saying it is incomplete |

Add `LemonCrow Review` to your branch protection's required checks if you want it to block.

### The hidden state

The summary ends with `<!-- lemoncrow:state v1 {json} -->`. Read it instead of scraping the text. Schema version 1:

```json
{
  "schema": 1,
  "pull_request": "acme/widgets#7",
  "head_sha": "…", "base_sha": "…", "merge_base_sha": "…",
  "depth": "auto", "model": "openai/gpt-4o-mini", "complete": true, "reviews": 2,
  "verdict": {"p0": 0, "p1": 1, "p2": 1},
  "findings": [{"id": "finding-…", "fingerprint": "fp-…", "severity": "p1", "state": "new",
                "path": "src/auth.py", "line": 2, "end_line": 4, "title": "…", "thread_id": 1002}],
  "usage": {"calls": 2, "input_tokens": 12000, "output_tokens": 800, "cost_usd": 0.004},
  "usage_total": {"calls": 5, "input_tokens": 30000, "output_tokens": 2000, "cost_usd": 0.01}
}
```

A finding's `state` is `new`, `carried`, `fixed` or `stale`. `units` and `units_truncated` hold what the next run compares against. Only a comment from a `Bot` account is trusted as this state.

## Pushes cost less

On a push, the Action reads the previous state, compares the new commit with it, and asks the model only about the targets whose code changed:

- findings on untouched code are **carried forward**: no model call, no new comment;
- a finding on changed code is looked at again; if it is still there it keeps its thread, if the changed code no longer has it, it is **fixed**;
- a fixed finding gets a reply on its thread, `Fixed in <short sha>`, and the thread is resolved when the token allows it (GitHub's `resolveReviewThread`). If it does not, the reply stays and the review still completes;
- a finding whose code left the pull request is **stale**;
- if the model call for a changed target fails, the finding stays, marked unverified, and the target is retried on the next push.

A push that changes nothing LemonCrow reviews makes no model call and posts nothing. A changed depth, rule set, content-access policy or review prompt re-reviews everything once, because last time's result no longer means the same thing.

## Cost and limits

- Every review runs under a hard cap of 20 model calls, and Auto typically uses a few. Formatting-only and comment-only changes cost zero calls.
- Lower the cap, or pick the depth, in `.lemoncrow/review.yaml`. It is read from the pull request's **merge base**, never from the pull request's own branch, so a pull request cannot loosen its own review:

```yaml
# .lemoncrow/review.yaml
review:
  effort: auto
  budget:
    per_review_model_calls: 6
    # per_review_model_cost_usd: 0.25   # per model call; see below
rules:
  - stable_key: auth-required
    instruction: Every handler under src/api must check the caller's identity before touching data.
    paths: ["src/api/**"]
ai_content_access:
  - mode: ai_denied
    paths: ["secrets/**", "**/*.pem"]
```

- What leaves your runner is the changed code of the targets the model is asked about, with a few lines of context, sent to the provider whose key you gave. `ai_content_access` keeps paths away from the model; a denied path is still part of the deterministic review.

- A dollar ceiling already exists as `per_review_model_cost_usd` (and the `LEMONCROW_REVIEW_INTELLIGENCE_MAX_COST` variable). It applies to each model call and needs the provider to report cost, which the LiteLLM route (Anthropic) does and the OpenAI route does not: on a route that reports no cost, a ceiling stops every call. Use the call cap there.

## Pull requests from forks

GitHub gives `pull_request` runs from a fork no secrets and a read-only token, so there is nothing to call a model with and no permission to comment. The Action detects this, prints a notice and exits 0.

Do not work around it with `pull_request_target`. That event runs with your secrets and a write token, and it is the event people pair with a checkout of the fork's code; the combination lets a pull request run its own code with your credentials. The Action refuses to run on it and fails with an error saying so.

To review a fork's pull request deliberately, run the workflow by hand against your own trusted checkout. Never check out the pull request's head:

```yaml
on:
  workflow_dispatch:
    inputs:
      pr:
        description: Pull request number
        required: true
# …the same job, with:
#   - uses: actions/checkout@v4          # your default branch, not the fork
#       with: {fetch-depth: 0}
#   - uses: lemoncrow-lab/lemoncrow@v1
#       with: {pr-number: "${{ inputs.pr }}"}
```

LemonCrow fetches the pull request's git objects and sends the diff to your model; it does not run it. Send only pull requests you are willing to show your model provider.

Dependabot runs also have no secrets: add the key under Dependabot secrets to review them, or they are skipped with a notice.

## Try it locally first

The same command reads findings without posting anything, using your own model key (or a local agent CLI):

```bash
export OPENAI_API_KEY=…
lc review --pr 123 --ai                      # print the findings
lc review --pr 123 --ai --json               # machine-readable
lc review --pr 123 --publish-bot --dry-run   # the exact comments the bot would post
```

`--publish-bot` itself runs only in GitHub Actions: on a laptop your `gh` login is a person, and AI output must not be posted under a person's name. This is the full Review CLI (`uv run lc review` in a source checkout).

## How it installs

LemonCrow is not on PyPI. The Action checks out itself at the ref you named, installs that checkout from its lockfile with `uv sync --frozen` into a private environment, and runs `lemoncrow review` from there. It installs only the client library for the route you use (`openai` or `litellm`). This repository's root `action.yml` is the Action, so `uses: lemoncrow-lab/lemoncrow@<ref>` is the whole install.

## Troubleshooting

- **`No model credentials`**: the key reached the workflow but not the step. Pass it in the step's `env:`, as above.
- **`Resource not accessible by integration`** in the run log: the workflow lacks a permission. The review still completes; the note names what it could not do (a status needs `statuses: write`, comments and thread resolution need `pull-requests: write`).
- **Nothing happened on a pull request**: check the run's notice. Forks, drafts and `on-push: false` runs say so.
- **A push posted nothing new**: the changed code produced no new finding. The summary's status strip says what was re-reviewed.
