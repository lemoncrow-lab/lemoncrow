# Review benchmark: cost, catch and noise on real pull requests

The Gate I harness ([review evaluation](review-evaluation.md)) replays 16 synthetic cases offline and has never seen a real provider. This benchmark is the other half: it runs the pull request reviewer, the one the [GitHub Action](../setup/github-action.md) runs, on real pull requests with a real model, and writes down what it cost and what it found.

**Status: the harness is built and tested end to end with a scripted model. No live run has been done, so this repository holds no cost per review, no catch rate and no comparison with another reviewer.** Every number a report prints says which driver made it, and a report from anything but a live run says "do not quote it" at the top.

## Run it

You need a model key, `git` with network access to the repositories in the corpus, and `uv`. Nothing else leaves your machine except the model calls and the git fetches.

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # or: export OPENAI_API_KEY=sk-...

# 1. See what it would do. Calls no model and fetches nothing.
uv run lemoncrow dev eval review benchmark --corpus evals/review/real_prs.yaml \
    --depth quick,auto,deep --max-usd 20 --plan

# 2. Run it, with a ceiling.
uv run lemoncrow dev eval review benchmark --corpus evals/review/real_prs.yaml \
    --depth quick,auto,deep --max-usd 20
```

`lemoncrow dev eval review-benchmark` is the same command. It writes `evals/review/reports/<corpus>-<driver>-<time>.md` and `.json`, prints a summary, and exits 0 when it finished, 1 on an error, and 3 when the spending ceiling stopped it (the partial report is still written).

| You set | The route is |
| --- | --- |
| `ANTHROPIC_API_KEY` | LiteLLM on `anthropic/claude-sonnet-4-6` (`LEMONCROW_REVIEW_MODEL` changes the model). Anthropic reports cost with the response. |
| `OPENAI_API_KEY` | the OpenAI route on `gpt-4o-mini` (`LEMONCROW_REVIEW_MODEL` changes it). OpenAI reports no cost, so dollars come from the local price table or from `--input-usd-per-mtok` and `--output-usd-per-mtok`. |
| both | Anthropic, unless `--provider openai`. |
| `LEMONCROW_LLM_BACKEND=…` | what you named; the benchmark never replaces it. |

Both defaults are provisional: the point of the benchmark is to choose the default by cost per catch. Run it once per model you are considering and compare the reports.

### Capping spend

A live run refuses to start without a ceiling, refuses to run in CI or under pytest, and checks the ceiling before every call.

- `--max-usd 20` stops the run once the dollars spent reach 20. It needs every call to have a price: the provider's own figure, else the local price table for a model it knows exactly, else the rates you pass with `--input-usd-per-mtok` and `--output-usd-per-mtok`. A call with none of the three stops the run with a message, rather than being counted as free. With the OpenAI route and a model the price table lacks it refuses to start.
- `--max-calls 400` stops after that many model calls, whatever they cost. Use it with `--plan`, which prints the most calls the run can make: the investigator ladder per review (quick 1, auto 6, thorough 6, deep 10, under the product's hard cap of 20 calls per review), times the reviews, plus two reviews per simulated push.
- The ceiling is checked between calls. Calls already running when it is reached finish, so a run can pass `--max-usd` by what a few parallel calls cost (at most the investigators of the deepest depth). The report says so.
- Reviews run depth by depth across all pull requests, then the pushes, so a ceiling that stops the run leaves every pull request with the cheaper depths. A stopped run is a partial report; it is never filled in.
- The benchmark clears `LEMONCROW_REVIEW_INTELLIGENCE_MAX_COST` for the run: that per-call ceiling stops every call on a route that reports no cost, and the benchmark has its own.

Start with `--max-usd 2 --depth auto --pr black-3564` to see the first numbers before spending more.

## What it runs

For each pull request and each depth it fetches the pinned `base_sha` and `head_sha` (shallow, by commit), captures the range into a fresh store, and runs `engine.run_ai_stage`: the Action's own code path, with its run plan, hard call cap, `ai_content_access` policy and [repository context](../setup/github-action.md#repository-context). Each review gets a fresh store, so no result is reused between depths.

- **A depth is chosen the way a customer chooses it.** `quick` is `review.effort: quick` in the trusted base's `.lemoncrow/review.yaml`; the benchmark commits that file into a throwaway copy of the range, on both sides, so the diff under review is unchanged. `auto` is the default. `thorough` and `deep` are the Action's `depth` input.
- **Quick often reviews nothing.** The product does not pick one of several independent targets arbitrarily, so on a pull request with more than one risk candidate Quick makes no call and reports `quick depth cannot cover N risk candidates`. The benchmark scores that review as incomplete, not as a miss.
- **Triage comes first.** A pull request with no risk signal in the changed paths costs zero model calls and cannot have its bug caught. The report counts those (`No-call reviews`) and says, for each missed label, whether the model was ever asked about its file.
- **A follow-up push** is the fix commit's source edits applied on top of the reviewed head (`pushes:` in the corpus). The push is reviewed twice, in separate stores: once with the first review's state carried forward (re-review only what changed, what the Action does) and once with nothing carried (re-review everything, what a per-push reviewer does). The difference is the extra cost per push with and without carry-forward. The patch is applied as written, else with a three-way merge, else without context; the report says which, or why it could not be built.
- `--no-impact` skips change-impact capture; the Action does it, so the default does too.

## What it reports

Per pull request and depth, and in aggregate over the depth:

| Figure | Meaning |
| --- | --- |
| model calls, input and output tokens, `provider_usd` | from the calls the review made. Tokens or dollars a provider did not report make that review's figure `null` and it is left out of the mean and counted in the checklist; they are never read as zero. `usd_sources` says whether a dollar figure is the provider's, the price table's or yours. |
| context tokens | LemonCrow's estimate of the [repository context](../setup/github-action.md#repository-context) in the prompts. It is part of the input tokens, shown so its cost is visible. |
| wall seconds, model seconds | elapsed time of the AI stage; the sum of each call's latency. In a replay, wall time is the replay's and is not comparable. |
| findings by P0, P1, P2 | the severities the bot would post. |
| catch rate | labels caught / labels, over labelled pull requests only, with a 95% Wilson interval. With nine pull requests the interval is wide, on purpose. |
| noise rate | findings that match no label / findings, over labelled pull requests. Labels are bugs a later commit fixed, so this is an upper bound on false positives: a real bug nobody fixed counts as noise. `duplicates` is the part that repeats a bug already caught (the same problem reported on two targets of one function). |
| cost per catch | dollars on labelled pull requests / labels caught. |
| push cost, with and without carry-forward | calls, dollars and re-reviewed targets for the push, both ways, and how many findings the push closed (`fixed`). |
| reference team | a projection: 10 developers, 300 pull requests a month, 1.2 reviews each, from this report's own means (`--team-*` change it). Not a quote. |

**How a finding catches a label.** Same file, and either (`location`) its changed region lies within the corpus `line_tolerance` of a line the fix edited, or (`symbol`) its text names an identifier the fix edited, which is how a bug outside the changed lines (a caller left behind by a signature change) can be caught. Assignment is one to one. `match: location` in the corpus file is the strict reading. Each catch in the report says which rule fired, and same-file findings that matched nothing are listed as near misses for a person to read.

## The corpus

`evals/review/real_prs.yaml` is the public seed: nine commits from well-known repositories that shipped a bug a later commit fixed. It follows Gate I's corpus vocabulary (`provenance.source: public_repository`, permissive licence, repository and commit) and the format is documented in its header and in `evaluation/real_prs.py`.

| Id | Repository | Licence | The fix commit (its subject, verbatim) | Attribution | Fix site in the reviewed head |
| --- | --- | --- | --- | --- | --- |
| `black-3564` | psf/black | MIT | [Fix bug introduced in #3564. (#3615)](https://github.com/psf/black/commit/dba3c2695c59fdb11825dbdf8f3b0ab6e0b368b2) | fix message | `src/black/lines.py:601` |
| `click-2934` | pallets/click | BSD-3-Clause | [testing/CliRunner: Fix regression related to EOF introduced in 262bdf0 (#2940)](https://github.com/pallets/click/commit/2a0e3ba907927ade6951d5732b775f11b54cb766) | fix message | `src/click/testing.py:119-125, 351, 360` |
| `attrs-1009` | python-attrs/attrs | MIT | [Fix backward compatibility with pickles before v22.2.0 (#1085)](https://github.com/python-attrs/attrs/commit/c9150d278ced32056d73c239ff64034e5056bcb6) | fix message | `src/attr/_make.py:934-936` |
| `black-4332` | psf/black | MIT | [Fix another f-string regression (#4339)](https://github.com/psf/black/commit/455de7703ea2c4144f8c1bb2c352974ac9b2403f) | blame | `src/blib2to3/pgen2/tokenize.py:625-628` |
| `requests-2845` | psf/requests | Apache-2.0 | [Fix regression from #2844 regarding binary bodies.](https://github.com/psf/requests/commit/fc8fa1aa265bb14d59c68eb68a179bce17953967) | blame | `requests/models.py:84, 387` |
| `requests-no-proxy` | psf/requests | Apache-2.0 | [Fix regression in API caused by commit 85400d8d…](https://github.com/psf/requests/commit/a9470fe31f0c9590dafeac4b53f91fbf11fd6677) | fix message | `requests/utils.py:637` |
| `httpx-408` | encode/httpx | BSD-3-Clause | [Fix wrongly deleted params introduced #408 (#410)](https://github.com/encode/httpx/commit/ea137a96d904bdf1ea5a9eff1342b4d3a9ac5b30) | fix message | `httpx/api.py:264-265` |
| `fastapi-12129` | fastapi/fastapi | MIT | [🐛 Fix form field regression (#12194)](https://github.com/fastapi/fastapi/commit/88d4f2cb1814392f54011b2bbd3fe55c5f2a3278) | blame | `fastapi/dependencies/utils.py:791` |
| `jinja-scoping` | pallets/jinja | BSD-3-Clause | [Fix regression in 2.9 involving unsafe Context.get_all() usage](https://github.com/pallets/jinja/commit/60a7e775d487c8de9f1b0f23c91ca9845c161fc2) | fix message | `jinja2/debug.py:201` (a file the pull request did not touch) |

These are Python projects, small to medium changes, and bugs that someone noticed and fixed: a deliberately narrow sample. It says how the reviewer does on that, not on your code.

### Where a label comes from

A label is ground truth only if nobody had to decide it. So nothing is written by hand. For a fix commit F and the change that introduced the bug B, `derive-label` does three mechanical things: it takes the lines F removed or changed; maps them back to the reviewed head of B by the diff between the two (a line that diff changed is dropped, not guessed); and keeps only source files, not tests, docs or changelogs. The `summary` is F's subject, copied. `attribution` records why B is the culprit: F's message names it (`fix_message`), or `git blame` attributes the lines F removed to B (`blame`). A pair with neither is refused as not evidence of a regression. `symbols` are the identifiers on the lines F edited, trimmed by a person to the distinctive ones.

To add a case, clone the repository (a blobless clone is enough: `git clone --filter=blob:none URL`), find a fix whose message names a regression (`git log --grep regression`), then:

```bash
uv run lemoncrow dev eval review derive-label --repo ./clone --fix <fix sha> --introduced <introducing sha, or the merge commit of a branch> \
    --id my-case --repository OWNER/NAME --pull 123 --title '…' --license MIT --symbols distinctive_name
```

and paste the entry into the corpus. Add `--within-change` when the fix is wider than the pull request: it keeps only the fix sites on lines the reviewed change itself added or changed (the diff between its base and its head), so a fix that renames a method in nine files does not label the eight the pull request never touched. Take the licence from the repository's `LICENSE` file at the reviewed commit. A pull request whose bug you cannot trace this way stays unlabelled (`labelled: false`): it yields cost and findings but no catch or noise rate. A pull request you know to be clean says `labelled: true` with no labels, so that every finding on it counts as noise. Never write a label from a diff you read yourself.

## The seeded public bugs

A second kind of corpus starts from a published set instead of from the history: the seeded-bug set that a commercial code-review vendor released with its 2025 benchmark. It is five repositories (Sentry, Cal.com, Grafana, Keycloak, Discourse), ten pull requests each, every one a historical upstream change re-opened as a pull request in a fork. Reviewing the same changes lets LemonCrow's catch, noise and cost be set beside what that vendor's reviewer did on them.

| File | Visibility | Repositories | Pull requests | Labels |
| --- | --- | --- | --- | --- |
| `evals/review/seeded_public_bugs.yaml` | `public` | keycloak/keycloak (Apache-2.0, Java) | 4 | 7 |
| `evals/review/seeded_public_bugs_nonpermissive.yaml` | `private`, not in the public mirror | getsentry/sentry (Python), calcom/cal.com (TypeScript), grafana/grafana (Go), discourse/discourse (Ruby) | 14 | 17 |

The split is the licence rule: a public corpus carries only permissively licensed repositories, so the other four are `visibility: private` (accepted by the loader for public-repository provenance of any licence; the file holds commit ids, line numbers and fix subjects, no source, and `scripts/public-paths.txt` leaves it out of the mirror). Write its reports outside the checkout with `--out-dir`.

What is the same as the vendor's set: the change under review. Each entry pins the upstream `base_sha`..`head_sha` of the historical change (patch-identical to what the fork's pull request shows). What is not: the labels. The vendor scored one hidden bug per pull request and has not published which; here a pull request is labelled with the later upstream fix or fixes that edited lines it added (`derive-label --within-change`, so `attribution` is `blame` or `fix_message`), and an entry exists only where such a fix exists. Eighteen of the fifty pull requests have one; the rest were planted by the vendor (no upstream fix exists), have no later fix that edits their lines, were reverted whole, or change only styles. No `pushes:` are simulated, because those fixes landed months later and do not apply on the reviewed head.

```bash
# What it would do: no model call, nothing fetched.
uv run lemoncrow dev eval review benchmark --corpus evals/review/seeded_public_bugs.yaml --depth quick,auto,deep --max-usd 20 --plan
uv run lemoncrow dev eval review benchmark --corpus evals/review/seeded_public_bugs_nonpermissive.yaml --depth quick,auto,deep --max-usd 20 --plan

# Run it (needs a key; start small).
uv run lemoncrow dev eval review benchmark --corpus evals/review/seeded_public_bugs.yaml --depth auto --max-usd 5
uv run lemoncrow dev eval review benchmark --corpus evals/review/seeded_public_bugs_nonpermissive.yaml --depth auto --max-usd 15 --out-dir ~/lc-bench-reports
```

Nothing has been run against a model. A catch rate from these files is not the vendor's published catch rate: that one counts a single hidden bug per pull request and ignores false alarms, this one counts every fixed bug it can pin and reports noise beside it, on eighteen pull requests, not fifty.

## The golden-comment benchmark

A public 50-PR benchmark set with an independently expanded answer key: five repositories in five languages (Python, TypeScript, Go, Java, Ruby), ten pull requests each, and for each pull request a list of the issues known to be in it (145 in the README of the key; its JSON files list 137), each with a severity. The question it answers is the one the other corpora cannot: how many of the listed issues does a reviewer's posted comments name (recall), and how many of those comments name a listed issue (precision), on the same 50 changes, whoever the reviewer is.

`evals/review/golden_comment_prs.yaml` is a `visibility: private` corpus: commit ids, titles and the address of each pull request's entry in the key, no source and no golden comment. It is left out of the public mirror. The key has no licence, so it is never copied into this repository: the corpus pins it by commit, and the scorer reads it from a checkout at that commit (`--golden-dir`, or cloned into the store at run time) and refuses any other.

### Scoring

For each pull request a judge is asked once which posted comments and which golden comments refer to **the same underlying issue**. Then:

- a posted comment is a **true positive** when it is assigned one to one to a golden comment, a **false positive** when it is not (the same issue said twice is one true positive and one false positive; the report also gives a `lenient` count that makes the repeat a true positive);
- a golden comment nobody was assigned is a **false negative**;
- a golden comment of severity `Low` is excluded from all three, and so is a posted comment that matches only a `Low` one;
- a comment that matches two golden comments counts once, and the assignment is the largest one-to-one matching, deterministic: most severe golden comment first;
- precision = TP / (TP + FP), recall = TP / (TP + FN), F is their harmonic mean, summed over pull requests first, and reported overall and per language and repository, with recall by golden severity.

What counts as **posted** for LemonCrow is what a user sees: an inline comment for each P0 and P1 finding and a line in the summary's list for each P2 (`--finding-threshold high` drops the P2). The cost columns come from the benchmark report of the same run: model calls, tokens, dollars (the provider's own figure) and wall time per pull request. A pull request whose review did not run, or whose judge failed, is listed as not scored and is in no total; it is never counted as zero.

The judge is fixed and recorded in every report: the claude CLI's `sonnet` alias with no tools and no settings, in an empty directory, one prompt (`golden-judge-1`, digest in the report), schema-checked JSON. It returns the pairs it considers the same issue; it does not choose the assignment. Its answers are cached by a digest of what it was asked (prompt version, model, the text of every posted comment, the whole golden set with severities), so scoring the same comments again is free and gives the same numbers. A different prompt or model is a different judge and is not comparable.

### Run it

```bash
# 0. What would be asked of the judge. No model call.
uv run lemoncrow dev eval review golden-score --corpus evals/review/golden_comment_prs.yaml \
    --report ~/lc-bench-reports/<name>.json --plan

# 1. Review the pull requests through the claude CLI route (scratch store; a ceiling is required).
export LEMONCROW_ROOT=~/lc-bench-root LEMONCROW_LLM_BACKEND=claude
uv run lemoncrow settings set intelligence.driver claude && uv run lemoncrow settings set intelligence.model sonnet
uv run lemoncrow dev eval review benchmark --corpus evals/review/golden_comment_prs.yaml \
    --depth auto --no-pushes --max-usd 40 --workdir ~/lc-bench-work --out-dir ~/lc-bench-reports --name golden-auto

# 2. Score the posted findings against the answer key.
uv run lemoncrow dev eval review golden-score --corpus evals/review/golden_comment_prs.yaml \
    --report ~/lc-bench-reports/golden-auto.json --depth auto --out-dir ~/lc-bench-reports
```

Another reviewer's comments are scored by the same command and the same judge: save them one file per pull request id (`{"comments": [{"id", "path", "lines", "text"}]}`) and pass `--reviewer-comments DIR --reviewer-label NAME`. Both reports go outside the checkout (`--out-dir` is required), because they hold the reviewers' comment texts. `--cached-only` never calls the judge.

### What it does not say

- Its pull requests were chosen by the set's author, and five of the fifty heads are single commits written for the set, so a reviewer that has seen the public repositories is not tested on unseen code.
- The golden comments are one team's judgement, extended by hand; a real defect the key does not list counts against the reviewer as a false positive.
- The judge is a model. Its decisions are in the report (`judge_pairs`, with a reason) and can be read; nothing here measures how often it is wrong.
- A saved comment of another reviewer is only as complete as the way it was saved; the report says when a page showed fewer comments than the reviewer's own summary counted.

## The first run: pull requests another reviewer already commented on

A customer's pull requests that another reviewer already commented on are a ready-made comparison on the same code: that other reviewer is the **baseline reviewer**. What you need from them is a list of pull request URLs and read access (`gh auth login` with an account that can read those repositories, and `git` able to fetch them, for instance after `gh auth setup-git`), the login the baseline reviewer's bot comments as (for example `review-bot[bot]`; there is no default, it is on the comments), and their agreement to use their pull requests: the corpus is an organization corpus, which records who approved it.

```bash
gh auth login
printf '%s\n' https://github.com/acme/shop/pull/101 https://github.com/acme/shop/pull/118 > ~/acme-prs.txt

# Writes the corpus (pinned heads and merge bases from GitHub) and each pull request's baseline reviewer comments.
uv run lemoncrow dev eval review init-corpus --pr-file ~/acme-prs.txt --out ~/acme/acme.yaml \
    --organization acme --approved-by "Pat Example" --reference "ticket ACME-12" \
    --reviewer-login 'review-bot[bot]'

uv run lemoncrow dev eval review benchmark --corpus ~/acme/acme.yaml --organization-opt-in acme \
    --out-dir ~/acme/reports --depth auto,deep --max-usd 20 \
    --baseline-usd-per-review 1.00 --baseline-label "Plan X, pricing page of 2026-10-20"
```

A customer corpus never lives inside this checkout, and its reports go outside it too (`--out-dir` is required).

- **The baseline reviewer's login.** `--reviewer-login` is required (repeatable) and has no default: the importer keeps only comments by the logins you name. If the pull requests show another author, the import stops and prints the authors it saw; pass the right login. To fetch one pull request by hand: `lc dev eval review import-reviews --pr acme/shop#101 --reviewer-login 'review-bot[bot]' --out baseline/pr-101.json`. To normalise an export you already have: `--from-json export.json` (with `--all-authors` instead of a login when you already filtered it). A corpus entry points at the file with `baseline: {comments_file: baseline/pr-101.json}`, or asks the run to fetch it with `baseline: {fetch: true}` and `--reviewer-login`. Public pull requests work without `gh`.
- **What is compared.** For each LemonCrow finding the importer pairs a baseline comment on the same file, within `--line-window` lines (default 8), whose text is similar enough (`--similarity`, default 0.2, over content words with identifiers kept whole). That gives `agreed`, `ours only` and `theirs only`; same-file nearby comments that were too different to pair are listed as near misses, for you to settle. The two defaults are a starting point. Look at the near misses before trusting a threshold.
- **Agreement is not accuracy.** Two reviewers can agree and both be wrong. To say which reviewer is right you need someone to judge the comments. Add `"verdict": "valid"` or `"invalid"` to a comment in its JSON file; the report counts judged comments, and the head-to-head claim stays "not ready" until they exist.
- **The baseline reviewer's price per review** is not in any report unless you pass it (`--baseline-usd-per-review`). It is secondary-sourced and has changed; check the pricing page and pass the number with a label that says where it came from.
- The seed corpus has no baseline comments. To compare on public code you would open the same changes as pull requests on a fork with the other reviewer installed and import those.

## Publishing a cost-per-review number

The landing page promises one "when early access opens". The report's last section, "Is a number ready to publish?", is the checklist, and it is deliberately hard to satisfy:

- the run is `live` (a scripted or replayed report never qualifies);
- it was not stopped by its ceiling;
- at each depth there are at least 8 complete reviews with a known cost, on at least 2 repositories, none incomplete, errored or skipped;
- no review had an unknown cost (the OpenAI route needs the price table or your rates);
- for a quality claim, at least 8 labelled pull requests scored at that depth; for a head-to-head claim, at least 8 pull requests carrying the other reviewer's comments, some of them judged.

When the cost block says "ready", build the sentence from its figures table and from nothing else:

- name the model and the date, and state that the figure is the mean (with median and p90) over this corpus, which is not the reader's code, and that it is the model's fees only, on the reader's own key;
- say which depth, and that a push costs less than a first review (give the carry-forward figure from the report, not a guess);
- do not publish another reviewer's price from the report: it holds only what you supplied, and that vendor's pricing must be checked on its live page;
- do not publish a catch or noise rate without its interval and its corpus, and do not publish "as good as" another reviewer from agreement alone.

Then, and only then, change the landing page's numeric rule (the test in the landing repository, `src/lib/positioning.test.ts`) and the claim rules in `docs/planning/2026-10-20-ai-pr-review-plan.md` section 2. This benchmark changes none of them by itself.

## Recording and replay

`--record-dir DIR` saves every model call (the exact prompt identity, the response, the tokens, the cost and the latency) one file per pull request. `--driver replay --fixtures-dir DIR` re-runs the whole benchmark from them with no model and no key, and gives the same findings and the same cost. A replay answers only an exact match: if the code, the depth, the prompt or the context changed, the call is reported as a miss ("no recorded call has this exact prompt", or "the pull request's pinned inputs changed since recording") and the review is incomplete; a stale answer is never served. Replays are for checking a change to the reviewer against an earlier live run without paying for it again. `--driver fake` runs a scripted stand-in that "finds" a bug where the changed code carries a `BUG:` comment; it checks the harness, and the tests use it.

## Files

| Path | What it is |
| --- | --- |
| `evals/review/real_prs.yaml` | the public seed corpus |
| `evals/review/seeded_public_bugs*.yaml` | the seeded-bug corpora: public (permissive licence) and private |
| `evals/review/golden_comment_prs.yaml` | the golden-comment corpus: 50 pull requests, private |
| `evals/review/reports/` | where reports go; ignored by git except its README |
| `src/lemoncrow/pro/capabilities/review/evaluation/real_prs.py` | corpus format and loader |
| `…/evaluation/label_derivation.py` | `derive-label` |
| `…/evaluation/benchmark.py` | the run: workspace, drivers, ceiling, reviews, pushes |
| `…/evaluation/scoring.py` | catch, noise and agreement |
| `…/evaluation/baseline_reviews.py` | the importer for a baseline reviewer's comments |
| `…/evaluation/corpus_init.py` | `init-corpus` |
| `…/evaluation/benchmark_report.py` | the JSON and markdown report, and the checklist |
| `…/evaluation/golden_comments.py`, `golden_judge.py`, `golden_scoring.py` | the golden corpus and answer key, the judge and its cache, the scorer and its report |
| `…/review/git_env.py` | the environment for git run on another repository (drops `GIT_DIR` and its relatives) |
| `src/lemoncrow/gateway/cli/commands/review_benchmark.py`, `review_golden.py` | the commands |
| `tests/gateway/test_review_benchmark*.py` | the tests, with a local git origin and a scripted model |
| `tests/gateway/test_review_seeded_corpus.py` | the seeded-bug corpora as data: they parse, are pinned and labelled, and obey the licence rule |
| `tests/gateway/test_review_golden_*.py` | the golden corpus and key, the matching rules, the judge cache and the command, with a fake judge |
| `tests/gateway/test_review_git_env.py` | a git command for a throwaway repository never follows an inherited `GIT_DIR` |

## Limits

- Nine pull requests with one label each is a pilot. The intervals show how little it settles.
- Catch is recall on bugs somebody found and fixed; noise is an upper bound. Neither says how many unknown bugs the reviewer misses.
- The reviewer's cost depends heavily on triage: a pull request with no risk signal costs nothing and catches nothing. Read cost and catch together, with the `No-call reviews` column.
- Dollars use the local price table or your rates; a price list changes, so a figure is for a model on a date.
- The baseline reviewer's comments are never ground truth, and the similarity threshold is a judgement.
