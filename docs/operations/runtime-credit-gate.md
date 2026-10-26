# Runtime Lite token and credit regression gate

This public release policy is self-contained. Historical consumption analysis and unqualified internal planning notes are intentionally excluded from the public mirror.

The Runtime Lite release gate must compare **the same coding tasks at the same
model** before and after an implementation change. Daily usage, search output
length, savings sidecars, and benchmark guesses are not substitutes for provider
usage plus verified task outcomes.

## What is guarded

`scripts/runtime_credit_gate.py` checks at least five paired, independently
verified coding tasks. It blocks a release if a previously passing task fails,
if the case/prompt/model/price basis changes, if recorded cost rises by over
10%, or if freshly processed input (ordinary input plus cache writes) rises
by over 15%. Both constraints are *aggregate across identical previously
successful tasks*. Cache reads and output tokens appear separately in the report.

The gate is registered as `runtime-credit` in `scripts/release_gate.py`.
It **fails closed when baseline or current recordings are missing**, rather
than treating missing measurements as $0 or green. The threshold does not
claim 30% savings; the gate prevents unapproved regressions.

A second release component, `runtime-credit-tests`, checks failure modes and
the Runtime Lite compatibility guards without making model calls. The gate
requires real coding-agent usage from **both** Runtime Lite builds; a provider
CLI connection probe or fabricated usage is not qualified task evidence.

## Obtaining evidence

`make runtime-credit-evidence OUT=<new directory outside the checkout>` does
all of the steps below on the prepared release commit: it runs
`scripts/runtime_credit_harness.py` (five pinned bug-fix tasks, each a fresh
`claude -p` session on `claude-haiku-5-5` whose only MCP server is that
build's `runtime lite-mcp`, checked by fixed assertions) against the last
release tag (`BASE=` overrides it) and `HEAD`, runs the gate on the two
manifests, and prints the two variables to export. It spends real model
usage, about $0.10.

1. Pin a repository fixture, task instructions, acceptance tests, tool policy,
   and model. Use independent verification with recorded output/artifacts.
2. Run the baseline Runtime Lite commit and the candidate build against the
   same five or more tasks. Use fresh coding sessions, comparable cache state,
   identical model and independent acceptance tests.
3. For each task, collect **provider-reported** ordinary input, cache-read
   input, cache-created input and output tokens; deduplicate provider message
   IDs across transcript snapshots. Record the actual billed USD, or use
   one fixed model rate card for both runs and mark `priced_tokens`.
   Do **not** substitute `est_output_tokens` from LemonCrow traces for
   provider tokens; that is only a character-based approximation.
4. Verify every task's correctness and record the verifier artifact/reference.
   Store the two evidence manifests outside tracked source files at
   `evals/runtime-credit/baseline.json` and
   `evals/runtime-credit/current.json`.
   The release gate runs a detached clean checkout; **give it absolute paths**
   to these untracked manifests with `LEMONCROW_RELEASE_RUNTIME_CREDIT_BASELINE`
   and `LEMONCROW_RELEASE_RUNTIME_CREDIT_CURRENT`. Their contents must be genuine
   and the gate validates them against the **exact release commit SHA**; never
   copy fabricated fixtures into the checkout.
5. Run:

   ```bash
   python -m scripts.runtime_credit_gate \
       --baseline evals/runtime-credit/baseline.json \
       --current evals/runtime-credit/current.json \
       --out reports/runtime-credit.json
   ```

   A full `make release-gate` also invokes the check, reading the two
   absolute paths supplied by those environment variables. Missing paths or
   measurements block release qualification before expensive suites start.

Each manifest must have:

- `schema_version: 1`, `run_id` (different for baseline and current),
  `runtime: "local_lite"`, `source: "provider_usage"`, and `commit`
  (full 40-character Git SHA).
- A nonempty `cases` array with distinct `case_id` entries, each containing
  the same `task_digest` (SHA256 of the task), `fixture_digest` (SHA256 of the pinned source/input fixture), `model`, and `cost_basis` on both sides. For `priced_tokens`, `pricing_revision` must also match (one fixed tariff on both sides).
- Each case has `verified: true`, explicit `passed` boolean, nonempty
  `verification_ref`, and positive `cost_usd`.
- Each case has `usage` with nonnegative integer `input_tokens`,
  `cache_read_input_tokens`, `cache_creation_input_tokens`, and
  `output_tokens` (not an aggregate combining these categories).

`cost_basis` is either `billed` (actual invoice/API billing) or
`priced_tokens` (estimated model-price-equivalent USD at a fixed rate card).
With a subscription credit limit, the latter is **not actual credits
consumed**. Report it as comparable provider-token cost, not as the user's
billed balance.

These manifests require external task execution and independent verification.
Do not manufacture a passing fixture from synthetic numbers or historical
sessions with unknown correctness. Unit tests use synthetic examples solely
to exercise the validator.

## Operational note: old MCP versus new index

Schema 3 is stored in `runtime-lite-v3.sqlite3`, separate from the
legacy `runtime-lite.sqlite3`. On first startup, a compatible legacy index
is copied with SQLite's online backup into the new schema-specific file,
without mutating the old file. Future schema revisions must change the
filename rather than migrate a shared older-process index in place.

An MCP process that was started before the upgrade can still hold its
old code in memory. Install a compatible runtime and restart the coding
host/session to reload it. Do not delete the index or forcibly kill other
agents. The protocol removes raw `structuredContent` from model-facing
responses; direct programmatic calls may still request full structured data.
