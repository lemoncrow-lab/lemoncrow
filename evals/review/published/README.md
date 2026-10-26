# Published code-review benchmark results

`code_review_benchmark.json` is the one source of the code-review benchmark table that appears in the repository README
(between `<!-- lemoncrow:benchmark:start -->` and `<!-- lemoncrow:benchmark:end -->`) and on the landing site
(`src/data/code-review-benchmark.json` in the landing repository, a copy). Nothing else holds a figure for it.

**While LemonCrow's block says `{"status": "pending"}` nothing is published.** The README section is empty and the
landing site renders nothing: a table of other tools' scores is never shown without ours. No LemonCrow figure is
estimated, defaulted or typed in by hand; the only way one reaches the file is a finished run (below).

```bash
uv run python scripts/render_code_review_benchmark.py --check     # valid? README (and landing) in step?
make benchmark-publish                                           # render README (+ ../landing if it exists)
make benchmark-publish RUN=path/to/run-report.json               # merge a finished run, then render
make benchmark-publish RUN=path/to/rescored-run.json RESCORED=<tool id>   # a run that scored another tool's own comments
make benchmark-publish LANDING=/path/to/landing                  # name the landing checkout
```

Publishing is refused, and nothing is written, if anything below does not hold.

## The dataset and the method

The benchmark is the public 50-pull-request set (five repositories, ten pull requests each) that one vendor published in
2025, scored against the expanded answer key that another vendor published in November 2025. The file pins the answer
key to a commit (`dataset.answer_key.commit`) and records the counts it was verified to hold: 137 golden comments,
40 of them Low severity, so 97 are scored. (The answer key's own README says 145; the files hold fewer, and the file
says so.) The method is the answer key's: an LLM judge decides whether a tool's comment and a golden comment refer to
the same underlying issue; Low-severity golden comments count as neither true positive, false negative nor false
positive; precision, recall and their harmonic mean (the F-score).

## Fields

| Block | Field | Meaning |
| --- | --- | --- |
| top level | `schema`, `updated` | schema version (1); date the file last changed |
| `dataset` | `name`, `pr_count`, `languages`, `repositories` | what was reviewed; `pr_count` is the sum of the repositories' `prs` |
| `dataset.origin` | `title`, `publisher`, `url`, `date`, `date_kind`, `verification` | where the pull-request set was published |
| `dataset.answer_key` | `name`, `publisher`, `repository_url`, `commit`, `commit_date`, `golden_comments`, `low_severity`, `scored_comments`, `readme_states_golden_comments`, `note`, `verification` | the answer key, pinned by commit, with the counts read from its files |
| `method` | `judge_rule`, `low_severity`, `metrics`, `source` | how a comment is scored, and where that is written down |
| `third_party[]` | `id`, `tool`, `precision`, `recall`, `f_score`, `reported_by`, `self_reported`, `scored_by`, `run_date`, `settings`, `source`, `verification` | a figure someone else published, never our measurement. `source` has `title`, `publisher`, `author`, `url`, `date`; `self_reported` is true when `reported_by` makes the tool |
| `third_party_discrepancies` | list of sentences | where the sources disagree with themselves or each other |
| `vendor_claims[]` | `id`, `tool`, `claim`, `value`, `measures`, `comparable`, `comparable_note`, `reported_by`, `self_reported`, `source`, `verification` | a vendor's headline figure that is not on the same scale, shown with why it is not comparable |
| `lemoncrow` | `status` | `pending` (and nothing else) or `published` |
| `competitor_rescored` | `null`, or `status` as above | the other tool's own comments, scored by our judge on the same pull requests (optional) |

`verification` is `{status, checked_on, note}`. `status` is `primary_read` when a person or tool read the source itself and
compared it with the file, or `search_retrieval` when the figures came from search-tool retrievals of it. **A published
table needs every `verification.status` to be `primary_read`**: the check refuses to publish figures nobody read at their
source. Today the third-party rows and the pull-request set's origin are `search_retrieval`, because the vendors' sites
were not reachable from the session that recorded them. To clear that, open each source, compare the numbers with the
file (and settle the discrepancies listed), then change the status and the note.

### A published `lemoncrow` block

| Field | Meaning |
| --- | --- |
| `status`, `measured_by`, `scored_by` | `published`, `LemonCrow`, `LemonCrow's judge` |
| `date`, `prs_reviewed`, `answer_key_commit` | the run's date; pull requests reviewed (must equal `dataset.pr_count`: a partial run is not published as the benchmark); the answer-key commit it was scored against (must equal the pinned commit) |
| `precision`, `recall`, `f_score` | percent, 0 to 100 |
| `counts` | `tool_comments_scored` and `tool_comments_matched` (precision's denominator and numerator), `golden_scored` (must equal `dataset.answer_key.scored_comments`) and `golden_matched` (recall's numerator). The three figures must follow from them, so a per-pull-request average is refused |
| `judge` | `model` and `prompt_sha256` (digest of the judge prompt) |
| `review` | `model`, `route`, `depth` of the review that made the comments |
| `engine` | `version` and `commit` (40 hex) of LemonCrow that ran |
| `cost` | `median_usd_per_pr`, `total_usd`, `median_calls_per_pr`, `total_calls`. A missing cost is not zero: omit nothing |
| `run_report` | `url` (a `.json` file directly under `evals/review/published/runs/` in the public repository) and `sha256` of that file; the check opens the file and compares |

A `competitor_rescored` block has the same fields except `review`, `engine` and `cost`, and adds `tool_id` (a
`third_party[].id`), `tool` and `comments_source` (`description`, `url`, `collected_on`: where that tool's comments were taken
from).

## The run report contract

A finished run is merged with `--from-run`. The file must be JSON with a top-level `benchmark_summary` object holding
exactly the published block's fields above, less `status`, `measured_by`, `scored_by` and `run_report`, which the importer
adds. `lc dev eval review golden-score` writes that object into its report (below). The rest of the file is the raw report; it is copied unchanged to
`runs/lemoncrow-<date>.json` (or `runs/rescored-<tool id>-<date>.json`), linked from the table and fingerprinted. A file
already there is never overwritten. The importer validates the merged result first and writes nothing if it fails.

### Where the summary comes from

`lc dev eval review golden-score` writes the `benchmark_summary` into its JSON report; nobody types one. It is written
when, and only when, the scoring is publishable, and every value in it is read from the report:

| Field | Taken from |
| --- | --- |
| `precision`, `recall`, `f_score` | the strict counts (true positives, false positives, false negatives), as percentages with two decimals; a comment that matched only a Low golden comment is neutral and in no count |
| `counts` | `tool_comments_scored` is true plus false positives, `tool_comments_matched` the true positives, `golden_scored` the counted golden comments, `golden_matched` those minus the missed ones |
| `date`, `prs_reviewed`, `answer_key_commit` | the date of the score report (UTC), the pull requests scored, the answer key's pinned commit |
| `judge` | the judge's model, and the full SHA-256 of its fixed prompt (the report's `prompt_digest` is the first 16 hex of it) |
| `review` | the benchmark report's model and driver (`route`), and the depth that was scored |
| `engine` | `commit` is the git head the benchmark ran on (read when the run started, as the benchmark recorded it); `version` is the LemonCrow version that scored the report |
| `cost` | the report's `cost` block: median and total dollars and model calls over the pull requests |

A report of another tool's comments is a `competitor_rescored` row. The command cannot know which tool the comments are or
where they came from, so you say so, with `--tool-id` (a `third_party[].id`), `--tool-name` (that row's `tool`, spelled the
same), `--comments-description`, `--comments-url` (https) and `--comments-collected-on` (YYYY-MM-DD). Without all five the
summary is left out and the report names the missing options. They are refused with `--report`.

**It is left out** (the JSON has no `benchmark_summary`, carries `benchmark_summary_omitted` with the reason, and the
Markdown report says it in one sentence under "Ready to publish") when any of these holds:

- a pull request of the corpus was not scored, the run was limited with `--pr`, or a review was partial (for another tool's
  comments, a truncated page that hid some of them is a partial review);
- the judge was not the fixed one, or the report does not carry the full digest of its prompt;
- for LemonCrow's reviews: the benchmark was not a live provider run, the findings were counted at a finding threshold
  other than the product's default, a review's cost is unknown (a missing cost is not zero), or the report does not name
  the review's model and route or the git commit it ran on, or the benchmark ran on a checkout with uncommitted changes to
  tracked files (`dirty` is true in the report's code block; a report without the field is accepted);
- precision or recall is undefined (no comment scored, or no golden comment counted), or the report's counts do not add up;
- for another tool's comments: any of the five options is missing or malformed.

The importer then checks the rest against this file: the pull-request count, the pinned answer key and the rounding. A
report made before the command wrote these fields has no summary; score it again (`--cached-only` answers from the judge
cache, so it makes no model call).

A raw report holds real model output. Whether one is published is a decision for the person who commits it; this
directory is mirrored to the public repository.

## What never goes in this file

An estimate, a projection, a figure from a partial run, a figure from a scripted or replayed run, or a competitor row
that was not published by its source. Another tool's figure is always attributed (`reported_by`), linked and dated.
