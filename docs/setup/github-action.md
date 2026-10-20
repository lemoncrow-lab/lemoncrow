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
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      # Pin a full commit SHA of the public repository (see "Installing a pinned version" below), not a branch or a
      # tag that moves: the commit is the version that reviews, and the prompt and checks come with it.
      - uses: lemoncrow-lab/lemoncrow@<commit-sha>  # pin to a release commit
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          # or: OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        with:
          # Advisory while you trial it: the check reports P0s but never fails. Once you have seen a few weeks of
          # pull requests and agree with the P0s it raised, change this to `p0` to fail the "LemonCrow Review"
          # check while a P0 is open (add the check to branch protection if you want it to block merges).
          fail-on: none

      # If the job is killed (timeout) or cancelled before the review finishes, the status it set to `pending` would
      # stay pending forever. This step runs only then, and turns a still-pending status into an error.
      - name: Do not leave LemonCrow Review pending
        if: failure() || cancelled()
        env:
          GH_TOKEN: ${{ github.token }}
          REPO: ${{ github.repository }}
          SHA: ${{ github.event.pull_request.head.sha }}
        run: |
          state=$(gh api "repos/$REPO/commits/$SHA/statuses" \
            --jq '[.[] | select(.context == "LemonCrow Review")][0].state // ""')
          if [ "$state" = "pending" ]; then
            gh api --method POST "repos/$REPO/statuses/$SHA" \
              -f state=error -f context="LemonCrow Review" \
              -f description="The review job failed or was cancelled before it finished. Re-run it." \
              -f target_url="$GITHUB_SERVER_URL/$REPO/actions/runs/$GITHUB_RUN_ID"
          fi
```

3. Open a pull request. The bot posts a summary comment and a `LemonCrow Review` status when the run finishes.

Notes on the workflow:

- **`permissions`** are the least the Action needs: `pull-requests: write` to comment and resolve threads, `statuses: write` for the status, `contents: read` to fetch the pull request. It posts as `github-actions[bot]` with the workflow's `GITHUB_TOKEN`; keep the default `github-token` input so the comments never come from a person.
- **`timeout-minutes`** bounds a hung model call; each call has its own 120-second limit, so a review that is not done in a few minutes is stuck. A large pull request can also run long: at Deep depth up to ten calls run one after another.
- **The last step** (`if: failure() || cancelled()`) is the fallback for a killed job. The Action sets `LemonCrow Review` to `pending` when it starts, and a job that is cancelled or times out never gets to replace it, which would leave a required check waiting for good. The step looks at the status on this run's head commit and, only if it is still `pending`, sets it to `error` with a message to re-run. It touches only its own commit, so a run that a newer push cancelled marks its own, older commit and nothing else. It is `error`, not a pass, on purpose: a review that never finished has not approved anything; change `-f state=error` to `success` if you would rather it stay out of the way. GitHub runs a `failure()` or `cancelled()` step after a timeout in the normal case; if the runner itself is lost, nothing can run, and the status stays pending until the next push or a re-run.
- **`concurrency`** per pull request with `cancel-in-progress` means a newer push cancels the review of an older one instead of queueing behind it.
- **`fetch-depth: 0`** gives LemonCrow the history to find the merge base. `ready_for_review` is in `types` so a draft is reviewed when it is marked ready.
- **`<commit-sha>`** is a placeholder: replace it with the full 40-character SHA of a commit of the public `lemoncrow-lab/lemoncrow` repository. There is no moving `v1` tag, on purpose. The Action installs LemonCrow from its own checkout at that ref, so the version you pin is the version that reviews, and a floating ref would change the prompt under you. See "Installing a pinned version".
- **Organization policy.** If your organization restricts which actions may run, allow `lemoncrow-lab/lemoncrow` and `astral-sh/setup-uv`; the Action uses the latter to install LemonCrow's dependencies (`uv` itself is pinned to an exact version). If it requires every third-party action to be **pinned to a commit SHA**, the Action already pins `astral-sh/setup-uv` to the commit of its `v5.4.2` tag. The sample workflow's `actions/checkout@v4` is a tag pin too; pin it to a SHA if your policy needs that.
- **GitHub rate limits.** When GitHub throttles a call (HTTP 429, or a 403 that names a secondary rate limit), the Action waits and tries again up to three times (5, 15 and 45 seconds, or the wait GitHub states, at most 60 seconds each), and never more than 120 seconds in total in one run. A permission error (`Resource not accessible by integration`) is not a rate limit and is not retried. If the limit outlasts that, the one write that was refused is reported in the run's notes and the next push repeats it.
- Runners: Linux and macOS (the install is POSIX shell); Windows runners are not supported. The key must reach the step as an environment variable; the Action never prints it.

### Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `github-token` | `${{ github.token }}` | Token the bot comments with. |
| `model-provider` | `auto` | `anthropic`, `openai` or `litellm`. `auto` uses `ANTHROPIC_API_KEY`, else `OPENAI_API_KEY`, from the step's environment. |
| `model` | the provider's own | Model id (also `LEMONCROW_REVIEW_MODEL`). OpenAI defaults to `gpt-4o-mini`; Anthropic to `claude-sonnet-4-6`. Both are provisional until review cost per catch is measured. |
| `depth` | `auto` | `auto`, `thorough` or `deep`. `auto` follows `review.effort` in `.lemoncrow/review.yaml` (Auto by default, `quick` if you set it there); the others go deeper for the run. |
| `fail-on` | `none` | `p0` fails the `LemonCrow Review` status while a P0 finding is open. |
| `max-usd-per-pr` | none | Stop reviewing a pull request once its reviews have cost this many US dollars in total (a number such as `2.50`). See "A budget per pull request". |
| `max-runs-per-pr` | none | Stop reviewing a pull request after this many reviews of it (a whole number such as `5`). |
| `review-drafts` | `false` | Review draft pull requests too. |
| `on-push` | `true` | Re-review on each push to an open pull request. `false` reviews only when it opens. |
| `pr-number` | the event's pull request | Set it for `workflow_dispatch` runs. |
| `working-directory` | `.` | Where the repository is checked out. |

LiteLLM users can set `model-provider: litellm` and the variables LiteLLM already reads for their cloud (`AWS_*`, `AZURE_*`, `VERTEXAI_*`) plus `LEMONCROW_LITELLM_MODEL`. `LEMONCROW_LLM_BACKEND` is respected if you set it yourself. The intelligence policy and its kill switch (`intelligence.enabled` in the LemonCrow settings, `LEMONCROW_ROOT` on a self-hosted runner) apply exactly as they do locally: when AI is off, the run exits 0, posts no comment, and sets the `LemonCrow Review` status to `skipped: AI review is turned off`.

## What the bot posts

**One summary comment**, found and updated in place by a hidden marker (`<!-- lemoncrow:summary v1 -->`), so a push edits it instead of adding another:

- a verdict line that agrees with the status: `1 must-fix before merge`, `Partially reviewed: 2 of 3 risk signals examined` when the review finished but left risk signals unexamined, `Review incomplete: <why>` when it could not finish, or `No must-fix findings` only when every risk signal was examined;
- a status strip: `Re-reviewed 38 changed lines · 1 new · 2 carried forward · 1 fixed since last review`;
- findings sorted by severity, each linking to its inline thread. **P0** must fix, **P1** should fix, **P2** optional;
- P2 findings in a collapsed block, `N low-priority findings held back`;
- on a clean review, a collapsed `What we checked`: files, rules applied, depth;
- a collapsed `Fix with your agent`: a prompt listing the open findings with `file:line`, and the checked replacement for each finding that has one, to paste into Claude Code, Codex or Cursor;
- a footer with the model, depth, model calls and tokens, how much of those tokens was repository context (`~3k of it repository context`, see below), and an estimated cost when LemonCrow's local price table knows the model. It never guesses a price: an unknown model shows tokens only.

**Inline comments** for P0 and P1 findings: a title with the badge, why, the consequence, and a one-click GitHub `suggestion` block only when the finding carries a fix LemonCrow has checked against the commit (see [Suggested fixes](#suggested-fixes)). P2 findings stay in the summary.

**Where a comment sits.** The investigator names the line, or short range (at most 8 lines), of the new file that the defect is on, and LemonCrow checks the claim before it places a comment by it: the lines must exist in the file at the reviewed commit, be lines the model was shown, and sit inside one hunk of the pull request's diff (the lines GitHub lets a comment attach to; GitHub's own diff is checked again when the comment is posted). If the claim fails any check, or the model named no line, the comment falls back to the first lines of the code the pull request changed in that function or file, which may not be the line that is wrong. The hidden state records which happened as `anchor`: `cited` or `target`. A comment cannot move once it is posted, so a finding that already has a thread stays on the lines the thread is on in the summary and the fix prompt, however the code shifts under later pushes; a finding with no thread follows its code.

A hidden JSON block closes the summary (see below), and every inline comment carries `<!-- lemoncrow:finding <id> -->` and `<!-- lemoncrow:fingerprint fp-… -->`. The fingerprint (category, code and rules) does not change when the model words the same problem differently. It is what stops a duplicate: right before posting, the bot looks for its own earlier comment on the same file with the same fingerprint, and if there is one it adopts that comment as the finding's thread instead of posting a second. That covers a run that `concurrency` cancelled after it posted a comment but before it saved the summary, which carries the state; a comment whose finding was closed (the bot replied that it was fixed) or that belongs to another finding is not adopted, so a bug that comes back, or a second bug in the same function, still gets its own comment.

Findings are AI output, so they are never posted as an approval or a change request: the bot's reviews use event `COMMENT` only, and human review state stays human. Anything the model wrote is neutralised before it is posted: it cannot forge the markers above, mention people, load images or open HTML blocks.

### Severity

One rule gives each finding its severity, and everything the bot shows reads that one answer: the badge on the inline comment, the verdict line, the held-back block, the `LemonCrow Review` status and `fail-on`. They cannot disagree, because none of them computes its own.

| Level | Meaning | What puts a finding there |
| --- | --- | --- |
| **P0** | Must fix before merge: a wrong result, a security hole, data loss | A violation of a rule you wrote in `.lemoncrow/review.yaml` (a critical rule) that cites that rule, or a **verified** finding the investigator rated **high** in the `correctness`, `security` or `persistence_migration` category. |
| **P1** | Should fix | Every other **high** finding (not verified, or in concurrency, API, performance and the like), and **medium** findings that are verified. |
| **P2** | Optional | Everything else, including every finding rated **low**, and a medium one with nothing but the changed code behind it. |

- **Rated** is the investigator's own `low`, `medium` or `high` for the impact of the defect on the code it cites. It only nominates a finding: it can lower one, and it can never make one P0 on its own. Its wording is never read, so a title that says "critical" changes nothing.
- **Verified** means evidence from beyond the finding's own changed code backs it, evidence the model did not write: a recorded check, an attached artifact, a static-analysis result, or an excerpt of a *different* file from the repository context pack (a caller the change breaks, a test, a definition). The finding's own changed unit and the exact source of that same unit do **not** count, because every changed unit comes with both and the model is shown those bytes anyway; neither does an excerpt of the finding's own file, or a related repository. A model's rating of `high` with nothing but the changed code behind it is therefore a **P1**, never a P0, and so never fails the check. What the Action can corroborate today is mostly the pack: it runs no scanner and records no checks of its own, so a P0 needs a finding whose cause the pack shows in another file (the stale caller of a function whose behavior the change broke), or a rule you wrote. Expect P0 to be rare; that is the point.
- **Unrated** findings (a model that gave no rating) keep the severity of their category: P1 for correctness, security, concurrency, persistence and API findings, P2 for performance and verification. Unrated is never low and never P0.
- A finding carried forward whose re-check failed on a push is **unverified** until a later check succeeds, so a P0 drops to P1 meanwhile. That review is incomplete, so with `fail-on: p0` the status is `error`, not a pass. A critical rule keeps its P0: your rule, not the model, made it a gate.
- **Rules**: every rule in `.lemoncrow/review.yaml` is a critical rule today (there is no per-rule severity yet), and a violation is P0 only when the finding cites the rule it violates, which the Action requires of every rule finding. A finding that names no rule is held to the ordinary ladder, P1 at most.

The categories that can reach P0 and the rule above are fixed in this release: there is no setting for them yet. `fail-on: p0` therefore gates your own rules and the findings LemonCrow could back with evidence. Start with `fail-on: none` (the sample's default), read the P0s for a few weeks, and switch to `p0` when you trust them.

### Suggested fixes

When a fix is a small, local and certain edit, the investigator may attach it to its finding: a replacement for up to 10 consecutive lines of the file. LemonCrow does not trust it. Before a finding may carry it, the replacement is checked against the exact commit under review:

- the lines it names exist in the file;
- they are the lines the model quoted (leading whitespace exact), so a miscounted range or a stale view is caught;
- they sit inside one hunk of the pull request's diff, the lines GitHub lets a comment attach to;
- the edit changes something and applies cleanly.

A replacement that fails any check is dropped and the finding stays, as an ordinary comment. A passing one is posted as a GitHub `suggestion` block, on a multi-line comment (`start_line` to `line`, right side) when it replaces more than one line, so the author can commit it with one click. GitHub's own diff gets the same check when the comment is posted. The same edit is listed in the `Fix with your agent` prompt.

Limits: a replacement is never offered for a file with CRLF or other ambiguous line endings, for a deletion, for more than 10 lines or 2,000 characters, or for deleted code. LemonCrow checks that the edit applies, not that it compiles or passes your tests: let CI judge it.

### The check

The `LemonCrow Review` commit status is set on the reviewed head, with a link to the summary comment:

| Situation | Status |
| --- | --- |
| No P0 finding, every risk signal examined | `success`, `No must-fix findings` |
| No P0 finding, but some risk signals were not examined | `success`, described `Partially reviewed: N of M risk signals examined` |
| A P0 finding and `fail-on: p0` | `failure` |
| A P0 finding and `fail-on: none` | `success`, described as advisory |
| Review incomplete (a model call failed, the call budget ran out) | `error` with `fail-on: p0`, otherwise `success` saying it is incomplete |

Add `LemonCrow Review` to your branch protection's required checks if you want it to block, and read "Required checks and skipped runs" below first.

#### Required checks and skipped runs

A required status check waits for a status with that exact name on the head commit. A run that does not review still has to answer, or the pull request shows `Expected: waiting for status` forever. So whenever the Action skips a pull request, it posts a `success` status with a description that starts `skipped:` on the head commit, when the token can write statuses:

| Skipped because | Status description |
| --- | --- |
| A draft (`review-drafts` is off) | `skipped: draft pull request` |
| A push, with `on-push: false` | `skipped: push to an open pull request, on-push is off` |
| The pull request is closed | `skipped: pull request is closed` |
| The intelligence kill switch is off | `skipped: AI review is turned off` |
| A Dependabot run with no model key | `skipped: Dependabot run without a model key` |
| A pull request from a fork | **none posted**, see below |

**Fork pull requests cannot be covered this way.** A `pull_request` run from a fork gets a read-only token, so it cannot write a status at all; the Action does not try, and prints a notice saying so. If you require `LemonCrow Review` and accept pull requests from forks, those pull requests will wait on it. Use a path of your own for them: do not make the check required, or let a maintainer run the workflow by hand for the fork's pull request (see "Pull requests from forks"; that run writes the status as the maintainer's workflow), or set the status from a trusted workflow of yours that is allowed to write it. The Action will not do this for you: the safe way to get a write token on a fork's pull request is the `pull_request_target` event, which it refuses.

Dependabot-triggered runs are not covered either by default. They have no access to repository secrets (no model key), and they are understood to get a read-only `GITHUB_TOKEN` as well; that is not something this guide has confirmed against GitHub's documentation, so check GitHub's documentation for Dependabot and GitHub Actions before relying on a status from a Dependabot run. If the token cannot write, the Action posts nothing, prints a notice, and still exits 0; a required check will then wait.

A skipped run is never a failure, and a failed attempt to post the skipped status is never one either.

**A partial review is `success` on purpose.** Auto examines at most two risk signals per run, so failing the check on every pull request with three or more would block merges for something its author cannot fix. What it will not do is look like full coverage: the headline, the check description and the status strip all say `Partially reviewed` and how many signals were reached, the check never says `No must-fix findings` for it, and a P0 found in the signals that were examined still fails the check under `fail-on: p0`. The unexamined signals are still owed: the next run (a push, or a re-run of the job) competes for the cap again until they are all examined. To examine more of them in one run, use `depth: thorough` or `deep`.

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
  "usage": {"calls": 2, "input_tokens": 12000, "output_tokens": 800, "cost_usd": 0.004, "context_tokens": 5200},
  "usage_total": {"calls": 5, "input_tokens": 30000, "output_tokens": 2000, "cost_usd": 0.01, "context_tokens": 11800}
}
```

`context_tokens` is LemonCrow's own estimate (four characters per token) of the repository context in the prompts; it is part of `input_tokens`, not on top of it, and it is absent when no context was sent.

A finding's `state` is `new`, `carried`, `fixed`, `stale` or `unreported` ("no longer reported"). A finding also carries `impact` (the investigator's rating), `verified` (evidence from beyond its own changed code backs it), `anchor` (`cited` or `target`, see above) and `target_line` (where its code's change begins, which later pushes compare against) and `anchor_digest` (a digest of the lines it cited, which a later push uses to tell that they changed) when it has them, and, when it has a checked replacement, `suggestion` with the `suggestion_start` and `suggestion_end` lines it replaces. `units` and `units_truncated` hold what the next run compares against. Only a comment from a `Bot` account is trusted as this state.

## Pushes cost less

On a push, the Action reads the previous state, compares the new commit with it, and asks the model only about the targets whose code changed:

- findings on untouched code are **carried forward**: no model call, no new comment;
- a finding on changed code is looked at again, and the model is shown the earlier finding and asked about it by id: still there, resolved, or unclear. If it is still there (reported again, or answered `still_present`) it keeps its thread;
- a finding is **fixed** only when the model says it is resolved **and** the lines the finding cited have changed (the text of those lines is looked for in the new file, so an edit above that only shifts them does not count). Anything else, such as a model that simply did not repeat itself, a harmless edit elsewhere in the same file, a finding that never cited a checked line, or a change small enough that nobody was asked, is **no longer reported**: it is closed, so it no longer counts toward the verdict or the check, but it is listed in its own block, its thread gets the reply `No longer reported in <short sha>` and stays open for you to resolve. If a later push reports it again it comes back on the same thread, not as a second comment;
- a fixed finding gets a reply on its thread, `Fixed in <short sha>`, and the thread is resolved when the token allows it (GitHub's `resolveReviewThread`). If it does not, the reply stays and the review still completes;
- a finding whose code left the pull request is **stale**;
- if the model call for a changed target fails, the finding stays, marked unverified, and the target is retried on the next push.
- Auto investigates at most two hypotheses per run, however many risk signals a large change has. One it did not reach is **pending**, not reviewed: the headline says `Partially reviewed: N of M risk signals examined`, the status strip says `N risk signals not investigated yet`, and the next push competes for the cap again. Ask for `depth: thorough` to investigate more of them in one run.

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

- What leaves your runner is the changed code of the targets the model is asked about, with a few lines of context, plus the repository context described below, sent to the provider whose key you gave. `ai_content_access` keeps paths away from the model; a denied path is still part of the deterministic review.

- A dollar ceiling already exists as `per_review_model_cost_usd` (and the `LEMONCROW_REVIEW_INTELLIGENCE_MAX_COST` variable). It applies to each model call and needs the provider to report cost, which the LiteLLM route (Anthropic) does and the OpenAI route does not: on a route that reports no cost, a ceiling stops every call. Use the call cap there.

### A budget per pull request

The call cap bounds one review, not a pull request: a long-running pull request is reviewed on every push. Two optional inputs cap its total, read from the usage the summary's hidden state already carries (`usage_total` and `reviews`), so there is no counter to host:

```yaml
        with:
          max-usd-per-pr: "2.50"   # the pull request's reviews may cost this much in total ...
          max-runs-per-pr: "6"     # ... and there may be at most this many of them
```

When either is reached, the next push is **not reviewed**: the model is not called, nothing new is posted, the summary's headline reads `Review budget reached: 6 reviews used of 6 allowed` (or `$2.51 spent of $2.50 allowed`) with a line saying the findings below are from the last review, and the `LemonCrow Review` status on the new commit is a `success` described `Review budget reached: …; this push was not reviewed`. A commit status has no neutral state, so that is the neutral one: a spent budget never blocks a merge by itself. An open P0 from the last review still does, under `fail-on: p0`, because it is still open. Raise the limit and the next run carries on from the last review's state, reviewing what changed since.

Limits to know:

- `max-usd-per-pr` needs a **known cost**: the one the provider reports (the LiteLLM route does), or a model the local price table knows exactly. If the cost is unknown it cannot be held to a dollar figure: the run says so in its notes and does not count it as spent. `max-runs-per-pr` needs no price.
- The total is what finished runs saved. A run cancelled by `concurrency` before it wrote its summary spent money that no state records, so it is not counted; neither is a pull request whose summary comment was deleted, which starts from zero.
- A rerun of a commit that was already reviewed is never a budget event: it costs nothing.

### Pin the Action during a trial

A LemonCrow release that changes the review prompt or the checks changes what "reviewed" means, so every open pull request is re-reviewed once on its next push, and a floating tag would do that without you choosing it. During a trial, pin `uses: lemoncrow-lab/lemoncrow@<full commit SHA>` (see "Releasing the Action" for where the SHA comes from), upgrade on purpose, and expect one re-review of each open pull request when you do. The Action installs LemonCrow from its own checkout at that ref, so the SHA pins the prompt, the checks and the dependencies together.

## Repository context

A model reached through an API key (OpenAI, LiteLLM, Ollama) gets one prompt and no tools, so on its own it sees the changed lines and a few lines around them. A bug in a *different* file, such as a function whose signature changed while a caller was not updated, is invisible to it. So before each investigation LemonCrow adds a small, deterministic **context pack** for the changed code, found by searching the exact pull request snapshot (no index to build, no extra model call):

| Kind | What it is |
| --- | --- |
| `caller` | code elsewhere that uses a changed or removed function, class or constant; files the pull request did not touch come first |
| `definition` | where the functions, types and constants the changed code uses are defined, when that is not already in the lines the model sees |
| `test` | tests that use the changed symbols, or are named after the changed module |
| `config` | configuration files that mention an environment variable the changed code reads |

The pack is cut to a token budget that grows with the depth, the same ladder as the investigator calls. These are **ceilings in estimated tokens per investigator call**, not measurements:

| Depth | Investigator calls (at most) | Context per call (at most) | Excerpts (callers / definitions / tests / config) |
| --- | --- | --- | --- |
| `quick` | 1 | 1,500 | 3 / 3 / 1 / 1 |
| `auto` | 2 | 3,000 | 5 / 5 / 2 / 2 |
| `thorough` | 4 | 5,000 | 8 / 8 / 3 / 3 |
| `deep` | 8 | 8,000 | 12 / 12 / 4 / 4 |

The 1/2/5/9 call ladder counts one synthesis call at `thorough` and `deep`; that call does not get a pack.

So context adds at most `investigator calls × ceiling` input tokens to a review (6,000 at `auto`, 20,000 at `thorough`, 64,000 at `deep`, and usually far less), and **never a model call**: the 1/2/5/9 investigator ladder and the 20-call cap are unchanged. When a pack is cut, the prompt says how many excerpts were left out, and the model is told that an absent caller proves nothing. The summary footer shows how much of the tokens was context, and the hidden state carries it as `usage.context_tokens`.

What it will not do:

- **Read secrets.** Files named like credentials (`.env`, `*.pem`, `*.key`, `id_rsa`, `credentials.*`, `secrets/`, `.aws/`, `.ssh/`), files holding a private key, symlinks and anything outside the checkout are never read; excerpts go through the credential redactor. `.env.example` style templates are read.
- **Bypass your content-access policy.** Every file is checked against `ai_content_access` for the model route in use. A path you deny, or mark `customer_local_only` on an external route, is neither searched nor shown.
- **Search build output.** `node_modules`, virtual environments, `dist`, `build` and `vendor` are skipped. A very large repository is searched nearest-first up to a file and size limit, and the pack says when it stopped.
- **Run on an agent CLI route.** `LEMONCROW_LLM_BACKEND=claude|codex|opencode` reads the repository itself with read-only tools, so it gets no pack.

Controls, as environment variables on the step:

| Variable | Effect |
| --- | --- |
| `LEMONCROW_REVIEW_REPO_CONTEXT=0` | no pack; the review is as it was before context existed |
| `LEMONCROW_REVIEW_REPO_CONTEXT_TOKENS=1200` | lower (or raise) the per-call ceiling for every depth |

Turning the pack on or off, or changing how it is selected, counts as a change to the review policy: the next push re-reviews everything once, because last time's result was reached with different information.

Limits to know: the search is textual, with tree-sitter to find the enclosing function, so it matches by name and can miss a caller reached through a dynamic dispatch or a re-export under another name; the per-rule questions (`rules:` in `.lemoncrow/review.yaml`) do not get a pack; and a change that touches one function can appear as several review targets (the symbol, the lines it replaced, the module declarations), all of which share the same callers, so the same problem can be reported once per target.

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
#   - uses: lemoncrow-lab/lemoncrow@<commit-sha>  # pin to a release commit
#       with: {pr-number: "${{ inputs.pr }}"}
```

LemonCrow fetches the pull request's git objects and sends the diff to your model; it does not run it. Send only pull requests you are willing to show your model provider.

Dependabot runs also have no repository secrets, so they are skipped with a notice. Adding the model key under Dependabot secrets is necessary to review them, and may not be enough: runs Dependabot triggers are understood to get a read-only `GITHUB_TOKEN`, in which case the review runs but cannot post a comment or a status. That is not confirmed in this guide; check GitHub's documentation for Dependabot and GitHub Actions and your workflow's permissions before relying on it.

## Try it locally first

The same command reads findings without posting anything, using your own model key (or a local agent CLI):

```bash
export OPENAI_API_KEY=…
lc review --pr 123 --ai                      # print the findings
lc review --pr 123 --ai --json               # machine-readable
lc review --pr 123 --publish-bot --dry-run   # the exact comments the bot would post
```

`--publish-bot` itself runs only in GitHub Actions: on a laptop your `gh` login is a person, and AI output must not be posted under a person's name. This is the full Review CLI (`uv run lc review` in a source checkout).

## Installing a pinned version

`uses:` needs a ref that exists in the **public** repository, `lemoncrow-lab/lemoncrow`. Use the full commit SHA of one of its commits:

- Open the repository's commit list (or its releases) on github.com, pick the commit you want to run, and copy its **full** 40-character SHA, not the 7-character short form: GitHub resolves only a full SHA in `uses:`.
- Or from a terminal: `gh api repos/lemoncrow-lab/lemoncrow/commits/HEAD --jq .sha` for the latest commit of the default branch, or `git ls-remote https://github.com/lemoncrow-lab/lemoncrow.git refs/tags/<tag>` for a release tag (an annotated tag is listed twice; the line ending `^{}` is the commit).
- The public repository's history is a filtered rewrite of the private one, so a SHA from the private repository does not exist there. Always take it from the public one.

Once a release tag exists you can pin that instead (`@v0.9.0`, for example), which is readable but moves if a tag is ever re-pushed; a SHA cannot. There is no `v1` tag to float on: the Release workflow requires a tag to equal the package version, so a `v1` tag would fail it.

Org policy and trial advice are in "Notes on the workflow" and "Pin the Action during a trial".

## Releasing the Action

For the owner of the repository. The Action runs from the **public** repository, so until the mirror has published it, no customer can use it. In order:

1. **Merge to `main`.** The Action (`action.yml`, the `pr_bot` package and the allowlist change) lives on a feature branch of the private repository until it is merged. Land it on `main` through the normal review.
2. **Run the mirror with `--resync`.** `scripts/public-paths.txt` was widened (it now allows `action.yml`), and an ordinary incremental mirror applies only the paths each new commit touched, so it would publish a fragment. From a checkout of `main`:

   ```bash
   make mirror ARGS="--resync"
   ```

   This publishes everything the allowlist newly allows as one new commit on top of the public `HEAD` (a fast-forward, no history rewrite). Add `ARGS="--resync --dry-run"` first to see the plan. `git ls-remote origin refs/mirror/last-pub` then prints the public SHA that run created.
3. **Check the public tree** has `action.yml`, `uv.lock`, `pyproject.toml`, `hatch_build.py`, `vendor/` and `src/lemoncrow/pro/capabilities/review/pr_bot/` (`uv run python scripts/mirror.py --export-dir <empty dir> --source-ref HEAD` materializes exactly what is published, and the Action's install command can be run against it).
4. **Pick the ref customers pin.** Either:
   - **a commit**: the public SHA from step 2 (or any later public commit). Nothing else is needed; customers use `lemoncrow-lab/lemoncrow@<that sha>`; or
   - **the next semver release**, for example `v0.9.0`, cut through the repository's normal release flow (`make release-prepare tag=v0.9.0`, then `make release-qualify tag=v0.9.0`, then `make release tag=v0.9.0`, which pushes, mirrors and tags). Do not cut a `v1` tag: `.github/public-workflows/release.yml` runs on every `v*` tag and `scripts/release_version.py check` requires the tag to equal the package version (`0.8.0` is not `1`), so it would fail the Release run. This change leaves the release process as it is.
5. **Try it once** from a scratch private repository on a real runner with `uses: lemoncrow-lab/lemoncrow@<the public sha>` before telling a customer to.

Until step 2 has run, `lemoncrow-lab/lemoncrow` on github.com has no `action.yml`, and any `uses:` of it fails with an error that the action cannot be found.

## How it installs

LemonCrow is not on PyPI. The Action checks out itself at the ref you named, installs that checkout from its lockfile with `uv sync --frozen` into a private environment, and runs `lemoncrow review` from there. It installs only the client library for the route you use (`openai` or `litellm`). This repository's root `action.yml` is the Action, so `uses: lemoncrow-lab/lemoncrow@<ref>` is the whole install.

## Troubleshooting

- **`No model credentials`**: the key reached the workflow but not the step. Pass it in the step's `env:`, as above.
- **`model calls failed`** with an Anthropic key: the LiteLLM route reads JSON out of a Markdown fence or surrounding prose and retries once, asking for only JSON, before it gives up, so a failure that persists is the provider's answer (an auth, quota or model-name error) and is in the run log. The Anthropic route has not been run against a live provider yet.
- **`Resource not accessible by integration`** in the run log: the workflow lacks a permission. The review still completes; the note names what it could not do (a status needs `statuses: write`, comments and thread resolution need `pull-requests: write`).
- **Nothing happened on a pull request**: check the run's notice. Forks, drafts and `on-push: false` runs say so, and all but forks also set the `LemonCrow Review` status to `skipped: <reason>`.
- **A push posted nothing new**: the changed code produced no new finding. The summary's status strip says what was re-reviewed.
