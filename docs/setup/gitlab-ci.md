# GitLab CI: AI merge request review

> **Early access.** `lemoncrow review --publish-bot` picks GitLab by itself in a GitLab CI job (`GITLAB_CI=true`) and reviews that job's merge request. It is tested against a recorded GitLab API, not yet against every self-managed version; see "Not yet supported" for what the GitHub Action does that this does not.

LemonCrow reviews each merge request in your own GitLab CI, on your own model key, and posts the result as a bot: one summary note with a verdict, one discussion on the line of each finding that matters, and a `LemonCrow Review` commit status on the head commit. It is the same reviewer as the [GitHub Action](github-action.md); this page covers what is different on GitLab.

Nothing is sent to LemonCrow. The job runs `lemoncrow review --pr "$CI_MERGE_REQUEST_IID" --ai --publish-bot` on your runner and talks only to your GitLab instance, to the model provider you give it a key for, to GitHub, which it fetches LemonCrow's public repository from, and to PyPI, which that repository's lockfile installs its dependencies from.

## Install

1. Create the bot's token and add it as the masked CI/CD variable `LEMONCROW_GITLAB_TOKEN` (see "Token").
2. Add your model key as a masked CI/CD variable (**Settings > CI/CD > Variables**): `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` together with `LEMONCROW_EXTRA` set to `cloud`.
3. Copy the job in [`integrations/gitlab/lemoncrow-review.gitlab-ci.yml`](../../integrations/gitlab/lemoncrow-review.gitlab-ci.yml) into your `.gitlab-ci.yml`:

```yaml
lemoncrow-review:
  image: ghcr.io/astral-sh/uv:python3.13-bookworm
  inherit:
    default: false
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event" && $CI_MERGE_REQUEST_SOURCE_PROJECT_ID == $CI_MERGE_REQUEST_PROJECT_ID
  interruptible: true
  variables:
    GIT_DEPTH: "0"
    DO_NOT_TRACK: "1"
    LEMONCROW_REF: "v0.8.2"
    LEMONCROW_EXTRA: ""
  script:
    - git init --quiet /tmp/lemoncrow
    - git -C /tmp/lemoncrow fetch --quiet --depth 1 https://github.com/lemoncrow-lab/lemoncrow.git "$LEMONCROW_REF"
    - git -C /tmp/lemoncrow checkout --quiet FETCH_HEAD
    - uv sync --project /tmp/lemoncrow --frozen --no-dev --no-progress ${LEMONCROW_EXTRA:+--extra "$LEMONCROW_EXTRA"}
    - >-
      uv run --project /tmp/lemoncrow --no-sync
      lemoncrow review --pr "$CI_MERGE_REQUEST_IID" --ai --publish-bot
      --depth "${LEMONCROW_DEPTH:-auto}" --fail-on "${LEMONCROW_FAIL_ON:-none}"
```

4. Open a merge request. The job runs in its merge request pipeline and posts a summary note and a `LemonCrow Review` status when it finishes.

The job runs only in merge request pipelines of your own project (the `rules:` line). LemonCrow is not on PyPI, so the first four lines fetch the public repository at `LEMONCROW_REF` (one commit, no history) and install it from its lockfile into `/tmp/lemoncrow`, outside your checkout; set `LEMONCROW_REF` to the release commit SHA the release notes print to pin the exact code, as the Action's `uses:` line does. `GIT_DEPTH: "0"` gives it the history the merge base needs. `inherit: default: false` keeps your pipeline's default image, `before_script` and cache out of it, so it runs exactly the lines above. `interruptible: true` lets a newer push cancel the review of an older head when **Auto-cancel redundant pipelines** is on. Set `LEMONCROW_DEPTH` (`auto`, `thorough`, `deep`) and `LEMONCROW_FAIL_ON` (`none`, `p0`, `p1`) as CI/CD variables to change them; they mean what the Action's `depth` and `fail-on` inputs mean.

## Token

GitLab's job token (`CI_JOB_TOKEN`) cannot post notes or set commit statuses, so the bot needs its own:

1. **Settings > Access tokens > Add new token** on the project: role **Developer**, scope **`api`**, an expiry date you will renew. (A group access token works the same way for every project in the group.)
2. **Settings > CI/CD > Variables > Add variable**: key `LEMONCROW_GITLAB_TOKEN`, the token as value, **Masked** on.

The notes and the status are posted as the token's bot user, never as the person who pushed. Before the review calls a model it checks the token's role on the project and stops with an error naming the fix when it is below Developer, so a token that cannot comment never pays for a review it cannot post. A token that is missing stops the job with the same kind of error. The job token is still what `git` uses to fetch the merge request.

**Protected or not.** A protected variable is only given to pipelines of protected branches, and a merge request from a feature branch is not one, so a protected `LEMONCROW_GITLAB_TOKEN` never reaches the review. Leave it unprotected and masked. That carries the usual GitLab trade-off, which holds for your model key too: anyone who can push a branch to this project can change `.gitlab-ci.yml` on that branch and read the variables its pipeline is given. Keep the token to this one project, at Developer and no higher.

**Where the token goes.** Only in the `PRIVATE-TOKEN` header of requests to your instance's API (`CI_API_V4_URL`), only over `https`, and never along a redirect. It is never put in a URL, a log line or an error message. An instance whose API is served over plain `http` is refused.

What the token is used for:

| Call | For |
| --- | --- |
| `GET /user` | who the bot is: only notes this user wrote are read as the bot's |
| `GET /projects/:id` | the token's role, checked before any model call |
| merge request, `diffs`, `commits` | the facts of the merge request, its diff lines, and its commit messages (for the independence line) |
| `notes` | the summary note: read, created, updated in place |
| `discussions` | one discussion per finding, replies, and resolving a fixed finding's discussion |
| `statuses/:sha` | the `LemonCrow Review` commit status |
| `repository/commits/:sha/statuses` | what CI reported on the head, for the summary's proof of work |
| `award_emoji` | the `eyes` award while the review runs, then `thumbsup` or `confused` when it ends |

## The status

`LemonCrow Review` is an external commit status on the reviewed head, posted into the merge request pipeline the job runs in (`CI_PIPELINE_ID`), so it shows next to your other jobs. A merged-results pipeline runs on a merge commit, not on the head; there the status is posted without a pipeline and GitLab files it under the head's own. It reads `pending` while the review runs, then `success` or `failed`, decided exactly as on GitHub (the Action guide's "The check"). GitHub's two red states, `failure` (a finding `fail-on` counts) and `error` (the review could not run), are both GitLab's `failed`; the description, up to 255 characters, says which.

To block merging on the review, turn on **Pipelines must succeed** (**Settings > Merge requests**) and set `LEMONCROW_FAIL_ON` to `p0` or `p1`: the job then exits 1 while a finding it counts is open, which fails the pipeline. Rely on that exit code, not on the status alone; GitLab has no setting that requires one named status the way a GitHub branch rule does.

**Proof of work** reads the head commit's statuses, which on GitLab include every CI job of the commit's pipelines. It leaves out the review's own job (`CI_JOB_ID`) and its own `LemonCrow Review` status, keeps only the newest result of each job name (a retried job is its retry), and never shows a result from another commit. A job that failed but is allowed to fail is listed as did not pass, not as a failure. A token that cannot read statuses gets `CI results not readable: the GitLab token needs the Reporter role and the read_api scope` in the summary, and the review is otherwise unchanged.

## What the bot posts

**One summary note**, found and updated in place by its hidden marker (`<!-- lemoncrow:summary v1 -->`), so a push edits it instead of adding another. Its content is the Action's summary comment (the Action guide's "What the bot posts"). Only a note written by the token's own user (its `author.id` is the id `GET /user` reports) is read as the summary: a note anyone else writes, marker and all, is ignored, so no one can feed the bot a state.

**One discussion per finding**, on the line of GitLab's own diff that the finding is on, with the same body as a GitHub inline comment. GitLab has no batched review, so each finding is posted on its own. Where GitLab differs:

- **A finding on several lines** is anchored at its last line, and its body says which lines it covers (`Lines 4-5`).
- **A checked fix** is a GitLab suggestion, `suggestion:-N+0`, which replaces exactly the lines the finding covers, ending at the line the discussion is on.
- **An unchanged line** in the diff gets both its old and its new line number, which GitLab needs to place a comment there.
- **A finding GitLab will not place** on a line is listed in the summary instead; a run cut short after posting some is picked up by the next run, which finds the bot's own discussions by their markers instead of posting them twice.
- **A merge request that moved on** while the review ran gets no new discussions on the old head; the next pipeline reviews the new one.

**When a finding is fixed** the bot replies once in its discussion and resolves it. A finding that is only no longer reported gets its reply and stays open for you to resolve.

## Merge requests from forks

By default GitLab runs a fork's merge request pipeline in the fork, without your project's CI/CD variables: there is no token to comment with and no model key. The template's `rules:` line leaves the job out of those pipelines; if it runs anyway, it prints a notice, exits 0 and posts nothing.

Do not work around it with **Run pipeline** in the parent project. That pipeline runs the fork's own `.gitlab-ci.yml` with your project's variables, so the fork's author decides what the job does with your token and key. LemonCrow refuses to run there and fails the job with an error saying so. To review a fork's change, read it yourself, or push its commits to a branch of your project after you have read them.

LemonCrow is installed from its public repository at the pinned `LEMONCROW_REF`, never from the merge request, and it does not execute the code it reviews: it fetches the merge request's commits as git objects and sends the diff to your model. Send only merge requests you are willing to show your model provider.

## Not yet supported

- **Learning from feedback.** Reactions on the bot's discussions, replies and resolved discussions are not read, so a GitLab review learns nothing from them (the Action guide's "Learning from feedback" is GitHub-only for now).
- **Chat.** `@lemoncrow` in a merge request comment does nothing: GitLab CI has no comment event to start a job from. Re-run the job to review again.
- **The human-review status.** GitHub gets a second status, `LemonCrow / human review`, that stays pending on a high-risk change. GitLab does not: a pending external status would hold the whole pipeline. The summary's "Human review required" line is still there.
- **Approvals on an older head.** GitLab's approvals name no commit, so the summary never says an approval was given on an older push. Turn on **Remove all approvals when commits are added** if a push should reset them.
- **Entire and Agent Trace provenance.** The independence line reads the commits' trailers only; the checkpoint and trace files are read on GitHub alone.
- **Multi-line comments.** A finding on several lines is anchored at its end line only; GitLab's line ranges are not used yet.
- **Self-managed GitLab older than 15.7.** The bot reads diffs from `GET /merge_requests/:iid/diffs`, which those versions lack.
