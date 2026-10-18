# `lc review`

**Understand what your coding agent actually changed — deterministically, before you commit it.**

Agents produce diffs faster than anyone can read them. `lc review` is the gate
between "the agent says it's done" and "you decide it's safe": it answers what
changed, what that change touches *outside* the diff, which agent session wrote
it, and what evidence exists that it was tested. No model is called, nothing is
summarized or paraphrased, and anything it cannot establish is printed as
`unknown` instead of guessed. The verdict is always yours — the tool never
prints `APPROVED`.

Two other pages cover the rest of the ground:

- **New to it?** [The walkthrough](review-walkthrough.md) builds a throwaway
  repo and runs the full loop — review, mark, comment, the agent revises,
  `--since-my-review` — in about ten minutes.
- **Need a flag?** [The CLI reference](cli.md#reviewing-agent-written-changes)
  lists every flag and every output field.

This page is the middle distance: what the tool is for, what the default
screen means, and the shape of the loop you'll actually run day to day.

**Which `lc`?** The installed client (`curl -fsSL https://lemoncrow.com/install.sh | bash`)
captures Reviews and opens the Review Reader; it accepts the range and capture
options in [Quick start](#quick-start) and `lc review --help`. In the Reader you
mark, comment, see what changed since your review and hand feedback to the
author. The scriptable flags on this page (`--mark`, `--comment`,
`--since-my-review`, `--feedback`, `--finish`, `--pr`, `--reproduce`,
`--check`, `data-flow`, `diagnostics` and the like) belong to the full Review
CLI, which a LemonCrow source checkout runs as `uv run lc review ...`.

---

## Quick start

```bash
cd your-project
lc runtime index        # once per repo — without it, ranking degrades to "biggest diff first"
lc review             # opens the Review Reader for uncommitted changes against HEAD
```

`lcr` is a real executable installed beside `lc` that runs `lc review`.

On a clean tree there is nothing to review, so it reviews the last commit
instead (`HEAD~1..HEAD`) and says so — the substitution is never silent. Other
ranges:

```bash
lc review --staged        # what you're about to commit
lc review --base HEAD~1   # a specific commit
lc review main..feature   # a branch, against main
```

## Continuous capture from a laptop, VPS or coding agent

The standalone hosted/public `lcr` thin client and `lc review` interface also
support `watch`; neither requires the coding agent's Runtime MCP session:

```bash
# Install the hosted thin client and authenticate once on this machine:
curl -fsSL https://lemoncrow.com/hosted.sh | bash -s -- --url https://api.lemoncrow.com
export PATH="$HOME/.lemoncrow/bin:$PATH"
lc auth login

cd your-project
lcr watch                         # monitors settled working-tree changes
lcr watch --staged                # staged-only updates
lcr watch --once --json           # explicit JSON checkpoint (sandbox/CI hook)
lcr watch --interval 1 --settle 2 # polling/settle bounds, in seconds
```

`lc review watch` is the same Review client path. The watcher stays in the
foreground; Ctrl-C stops it. An initial clean checkout does not create an empty
Review, but committing after an active Review advances its Snapshot. Review
updates appear as URLs (or JSON line events with `--json`). It does not execute
checks and does not open a new browser tab per edit. P1 deduplicates unchanged
source fingerprints but still submits complete head/base Views at capture; an
incremental *byte* protocol, persistent daemon, machine tokens and independent
sandbox verification are explicitly later deliverables.

**Data boundary:** only point Review at trusted checkouts whose tracked and
unignored contents may be sent to the configured server. Do not rely on
Runtime/MCP sync to discover edits for Review.

## What the terminal summary tells you

The Review Reader is the default human surface. Use `lc review --no-open` when
you specifically want the compact terminal summary instead:

```
ATTENTION
  ⚠ make_token changed   (src/auth.py)
    untouched call sites:
      src/session.py:L8    SessionManager.open

START HERE
  1. src/auth.py
     modified · production · +4 -2
     - 3 known callers
     - high-centrality symbol

EXECUTION EVIDENCE
  Generated with: unknown

Human review  REQUIRED
```

| Block | What it means |
| ----- | -------------- |
| `ATTENTION` | Changed definitions whose **callers were not touched** — a change leaking past its own diff. Plain `git diff` cannot show this; it comes from the local code index. |
| `START HERE` / `THEN` | The files in review order, ranked by risk (callers, centrality, blast radius), with the reasons attached to each row — never an unexplained score. |
| `EXECUTION EVIDENCE` | Which agent session produced this change and how that was established. `recorded at edit time` is a fact; anything else says so. |
| `Human review REQUIRED` | Always present. Nothing in this tool approves a change. |
| `degraded: …` | What it could not compute (usually: no index yet), named instead of silently skipped. |

## The review loop

A review is not a one-shot printout — it's a durable session keyed on
`(repo, subject, range)`, reopened rather than re-created on every run. That's
what makes the rest of this useful. In the Reader the loop is **Mark reviewed**,
**Add comment**, **Changed since your review** after the next `lc review`, then
**Copy feedback** or **Send to agent**. The full CLI scripts the same loop:

```bash
lc review --mark src/auth.py                          # record that you read this
lc review --comment "ttl is hardcoded" --on src/auth.py:L81 --comment-kind request_change
#   ... the agent revises the code ...
lc review --since-my-review                           # what changed since I looked?
lc review --feedback                                  # every open comment, as one bundle for the agent
lc review --finish                                     # record that you're done, with what's still open
```

A mark is bound to the *content* you reviewed, not a line number — ten lines
added above a function you approved don't reopen it; a rewrite of its body
does, and `--since-my-review` tells you exactly that. A comment is re-anchored
by its text and surrounding context on every later revision; if more than one
location could be right, it's reported **orphaned rather than guessed**.

**Send to agent** continues the author's own coding-agent session (Claude Code,
Codex, OpenCode, LemonCode or Copilot) with your feedback. The checkout and the
feedback can carry untrusted text, so the delivered session keeps that host's
normal approvals: LemonCrow never adds a bypass, skip-permissions or allow-all
flag. Codex runs headless in its `workspace-write` sandbox with network access
off; Copilot and OpenCode get no auto-approve, so they stop at their own
approval prompts.

## Reviewing a GitHub pull request

This uses the full Review CLI from a source checkout:

```bash
lc review --pr 123                       # or OWNER/REPO#123, or the PR URL
lc review --pr 123 --publish --dry-run   # what GitHub would receive
lc review --pr 123 --publish             # post it: one review, your `gh` login
```

Run it inside a clone of the PR's repository. LemonCrow fetches the PR head
through `gh`, diffs it against its merge base with the base branch (GitHub's
*Files changed*), and opens it like any other Review. A PR has one durable
Review: rerun after the author pushes and you get a new revision, with
`--since-my-review` working as usual.

Publishing posts your open comments and your verdict for the current revision
(LGTM becomes *Approve*, changes requested becomes *Request changes*). It is
also offered in the Reader after you finish, and from the review menu.

- GitHub only accepts inline comments on lines it shows in its diff. Anything
  else (file-level, orphaned, outside a hunk, a file with no diff) goes into
  the review body with its location. Nothing is dropped.
- Your own PR, or a closed one, is published as *Comment*, because GitHub
  refuses an approval there. The preview says so.
- If the PR has new commits, publishing stops until you refresh. Your comments
  never land on code you didn't see.
- Retrying is safe. Every review carries a hidden marker, so a retry after a
  timeout adopts the review GitHub already has instead of posting a second one.

No token is stored and no LemonCrow account is involved. Publishing runs only on
the local server, which acts as you.

## The browser workspace

```bash
lc review             # default: open the Review Reader
lc review --no-open   # capture and print the Review URL without opening a browser
lc review --open      # explicit form
```

The local workspace has a compact review outline, one continuous virtualized
multi-file diff reader, and Context on demand as an overlay/pinnable surface for
impact, checks, evidence, author context, and discussion. It reads and writes
the same durable session as the full CLI, so pressing `r` in the browser shows
up in `lc review --marks` there.

The Local Review server listens on loopback only (port 7420 by default). A
browser is connected once by `lc review --open`, which opens a short pairing
window; the browser then holds a Review-only session for that exact origin
(host and port) and renews it while it is still valid. If the browser reports
it is not connected, or its connection expired, run `lc review --open` again.
The server answers only a literal loopback `Host` on its own port
(`127.0.0.1:7420`, `localhost:7420`, `[::1]:7420`) and returns `403` for
anything else, which also blocks DNS-rebinding pages. An SSH tunnel therefore
works only with the same local port (`ssh -L 7420:127.0.0.1:7420 host`); a
different local port (`-L 9000:127.0.0.1:7420`) is refused.

## Machine-readable output

```bash
lc review --json    # the full packet, uncapped, for scripts and CI
lc review --html out.html
```

## Hermetic execution and reproduction

When Review runs reviewed code (previews, runtime checks, hosted worker
runs) it uses three sandboxed stages:

| Stage | Network | What runs |
| ----- | ------- | --------- |
| fetch | package registries on the trusted allowlist only | lockfile packages are downloaded and checked against their lockfile integrity; no lifecycle or install scripts run |
| build | none | the dependency tree is assembled offline |
| run | none | the command runs with recording sinks for SMTP, HTTP/webhooks, queues and object stores; nothing is delivered |

A preview whose dependency graph differs from the trusted checkout now runs
through these stages instead of being refused, and a runtime check provisions
the reviewed lockfile's dependencies the same way before it runs. Registries,
sidecars, fixtures and redaction come only from the trusted base
`.lemoncrow/review.yaml` (and the organization policy), never from the head:

```yaml
execution:
  registries:
    - host: npm.internal.example      # ecosystem: npm is the default
      scopes: ["@acme"]               # @acme/* may only come from here
    - host: pypi.internal.example
      ecosystem: pypi
      path_prefix: /simple
      packages: [acme-core]           # acme-core may only come from here
  fixtures: [fixtures/seed]           # directories taken from the base side
  redact: ["ACME-[0-9]{6}"]           # extra patterns, on top of built-in secret redaction
  redact_literals: ["fixture-password"]
  mask: [{x: 0, y: 0, width: 320, height: 40}]   # screenshot regions blacked out before storage
  retention_days: 14                  # raw artifacts; summaries persist
```

Registry allowlist rules:

- An ecosystem's public registry (`registry.npmjs.org` for npm; `pypi.org`
  and `files.pythonhosted.org` for Python) is allowed only while the trusted
  base declares no registry for that ecosystem.
- An organization `registries` list is a ceiling: a trusted-base registry it
  does not also list is dropped, and an ecosystem it does not list has no
  registry at all.
- `deny_all: true` in the organization policy (or the trusted base) allows no
  registry; any run that needs dependencies is `NOT_RUN`.
- Every lockfile entry whose resolved URL is not on the effective allowlist
  is refused, and so is a pinned name served by another registry.

Supported lockfiles:

| Ecosystem | Lockfile | Notes |
| --------- | -------- | ----- |
| npm | `package-lock.json` (v2/v3) | integrity-checked tarballs; install scripts never run |
| Python | `uv.lock`, or `requirements*.txt` where every line is `==`-pinned with `--hash=sha256:` | wheels only; the root project's dependencies and all dev groups go into a virtualenv first on `PATH` |

A package with no wheel for the sandbox interpreter (sdist only) is refused
with a `NOT_RUN` reason rather than built. pnpm, Yarn, Bun, Poetry, Pipenv,
PDM, Cargo, Go, Bundler and Composer lockfiles, and dependencies declared
without a lockfile, are refused the same way; a run never falls back to
whatever happens to be installed.

Hosted runs (`customer_worker` and `managed_sandbox`) go through the same
stages on the review worker. The control plane sends the trusted policy with
the job; the worker fetches from the allowlisted registries, builds offline
and mounts the result read-only into the run. The worker pod has no egress by
default, so a hosted run whose lockfile needs packages is `NOT_RUN` until the
operator lists the registry CIDRs in `reviewWorker.registryEgress`.
Trusted-base sidecars start beside the recording sinks on the worker.
Trusted-base fixtures are not shipped to hosted workers yet: a hosted run
with `fixtures` configured is `NOT_RUN` rather than run without them.

The run plan decides what executes. A runtime validation runs only when the
Snapshot's persisted effective run plan schedules its `surface:<id>` entry
and places it on the executor that would run it: `author_machine` runs
`local` entries, `customer_worker` runs `worker` entries and
`managed_sandbox` runs `managed` entries. On a self-hosted deployment the
`managed` plane is the customer's own worker. Hosted jobs are checked again
when a customer worker claims them, on each of its heartbeats and when the
managed sandbox executes them. A job whose plan changed while it was queued
is cancelled, not run, and one whose plan changed while it ran is asked to
cancel. Its result is marked superseded, not published.

Reviewer-requested executions go through the same plan. A surface run, a
surface or web preview, and a surface-impact comparison that runs or
renders both sides each add their entry as a manual pick before anything
executes. Picks only add work and never remove a policy-required entry, and
the response returns the cost delta. Surface previews map to their
`surface:<id>` entry, and web previews map to `visual.screenshot`. A
surface the trusted base does not configure is added as
`surface:<provider>:<id>`. `Run specific…` can preview and confirm that
entry too. The request is refused with `409` when the amended plan does not
run the entry on that executor, for example because its family is off or the
already planned can run there. A surface run always executes the runtime
defined by the trusted base: a runtime that only the Change adds is refused
with `422` and runs only after it lands on the base branch. Runs on this machine record compute per stage (`fetch`, `build`,
`run`, plus `setup` for the rest of the wall time) in the run plan's
compute view. Hosted jobs are metered from their leases and are not
recorded there, so nothing is counted twice.

Every result carries a versioned execution record
(`application/vnd.lemoncrow.execution-result+json;version=1`):

- `executor_class`: `author_machine` (self-attested; local runs and
  `lc review --check --run`), `customer_worker` or `managed_sandbox`. The
  dispatcher sets it, never the executor. A required check with
  `executor: trusted` is only satisfied by `customer_worker` or
  `managed_sandbox` results.
- `environment_fingerprint`: an opaque digest of the image digest, toolchain,
  lockfile digest and recipe digest.
- `identity`: subject (exact Snapshot side), executor class and environment
  fingerprint. Outcomes are not part of the identity.
- `verification`: a statement such as
  `managed sandbox · image sha256:… · network denied`, plus the integrations
  the sandbox cannot exercise (payment providers, third-party APIs,
  production data), listed as not verified.

Output, captured payloads and records are redacted before they are stored,
and models only see execution evidence through the repository's AI content
access policy. Each captured email, webhook, upload or queue body is kept in
full (redacted) as a `raw` retention artifact, referenced from the record as
`run-artifact:<review>:<sha256>`: beside the Review store on an author
machine, and in the organization's Review artifact storage on hosted runs.
Raw artifacts expire after the retention window unless a judgment, finding,
required check, human behavior judgment or human observation references
them.

To replay a recorded run locally:

```bash
lc review --reproduce ev_123          # evidence id on the configured server
lc review --reproduce record.json     # or a saved record
```

The replay is always `author_machine` and never writes caches that trusted
runs read. Its execution policy comes from your local trusted base (the
`.lemoncrow/review.yaml` committed at `--trusted-base`, default `origin/HEAD`,
else `HEAD`), never from the record. The record's policy is compared, and a
mismatch (a changed trusted base, or organization policy you do not have
locally) exits 1 unless you pass `--use-recorded-policy`, which replays under
the recorded policy and prints a warning.

The record never chooses what runs: its command must be one of the runtime
commands committed in the trusted base's `.lemoncrow/review.yaml`, and its
environment may hold only the variables LemonCrow's runners record. Anything
else is refused, with or without `--use-recorded-policy`. Committed symlinks are
materialized as plain files holding the link text, as in the Review snapshot.

The output says what the verdict rests on. A runtime check compares the
source tree digest and every stage outcome. A preview compares the source
tree digest and its dependency fetch/build stages; the preview's own build
and render stages are listed as not compared, because rendered output is not
deterministic.

## Side effects, weakened checks and agent actions

On every pushed Snapshot, Review statically reads the changed files (no code is
run) and adds what it finds to the Brief's **Behavior changes** section and the
review queue. Each finding is something to judge, not a verdict:

- **Side effects**: environment variables, config keys, outbound hosts, routes,
  CLI flags, telemetry events, cron jobs, migrations, database drops,
  permissions, install scripts, dependencies and personal or secret data
  reaching logs, telemetry, external endpoints, storage or a model prompt.
  Each is compared with the side-effect register of the base commit, so a
  Review shows only what is new, removed or changed. The register is derived
  from the base tree every time it is needed; nothing writes to it. A side
  effect that only moved between files is not reported. When the base tree
  cannot be read in full, the Brief says so and over-reports instead of
  missing something.
- **Weakened verification**: newly skipped or focused tests, removed or
  loosened assertions, new type/lint/security/coverage suppressions, lowered
  thresholds, grown ignore lists, removed or conditional CI steps and bulk
  snapshot updates.
- **Spec changes**: changed tests rendered as plain statements, for example
  `no longer checked: refund` or `new expectation: charge posts to vendor`.
- **Agent instructions and tool permissions**: changes to `AGENTS.md`,
  `CLAUDE.md`, `.cursorrules`, MCP server config, `.claude/` settings, prompt
  files and the head `.lemoncrow/review.yaml`. Their contents are shown as data
  and never applied, so text inside them cannot change how they are reviewed.

Weakened verification, agent-instruction changes and critical side effects
(permissions, database drops, install scripts, personal or secret data flows)
block auto-approval and are judged one at a time, never in bulk.

A dependency change is judged once, in the dependency review, and never again
as a side effect; the Brief notes how many dependency changes were left to the
dependency review, and readiness counts each one once.

The analysis is the `side_effects.static` entry of the run plan. Turning the
`static` family off in the trusted base `.lemoncrow/review.yaml` skips it; a
`.lemoncrow/review.yaml` changed by the Review itself cannot turn it off. It
runs once per Snapshot, and its compute is recorded with the Snapshot.

Static analysis never runs the reviewed code, so it runs where the Snapshot is
held: on your machine for a local Review, and on the Review server for a hosted
one. It does not use `run_on`, the managed sandbox or its enablement, and its
compute is labelled with the place it ran (`local` or `server`). A deployment
whose `allowed_run_on_by_class` places `static` on another executor, such as
`worker`, gets the entry as not available with that reason in the run plan;
when the entry is
required, that blocks readiness instead of passing silently. A hosted request
never waits for the analysis: it starts in the background, the Brief shows it
as running, and readiness stays pending until it finishes.

The estimate for the run plan's minute budget is 0.1 minutes per Snapshot until
the Review has measured a run, then this Review's average measured time per
run. A per-Review minute ceiling below the estimate limits the entry, and the
Brief says the inventory was limited by budget.

The Brief and Activity also list consequential actions taken outside the
repository by every development session linked to the change, including
read-only sessions that never became contributors: package installs, network,
cloud and database commands, secret access, git pushes and writes outside the
workspace. Routine work inside the repository, such as `npm install` from the
lockfile or running tests, is not listed.

## Observed side effects and rendering diffs

Static analysis reads code; these entries run it. Each runs only when the
Snapshot's effective run plan schedules it (when the plan does not, pick it
from the Checks rail), in the same sandbox as every other
run, with its compute recorded and its plan budget honored. Base and head are
compared only when both ran under one environment fingerprint; otherwise the
result says **not comparable** and nothing is reported as a regression.

- **Runtime side effects** (`side_effects.runtime`) runs the trusted-base test
  recipes on base and head with an observer: network attempts (the sandbox
  blocks them and records them), files written, processes started, SQLite
  queries and schema changes made from Python, and runtime errors. Only
  differences become Behavior changes. The observer uses a syscall tracer when
  the sandbox has `strace` and can attach; otherwise Python audit hooks, which
  see only what Python code does. A side effect that was not observed is not
  proof that it cannot happen. The same runs record calls into the changed
  files: in the architecture map's execution flow, a step those calls confirm
  is labelled **observed** and cites the run; every other step stays
  **inferred**.
- **Rendering diffs** (`visual.dom_a11y`, `visual.matrix`) render pages in a
  sandboxed headless Chrome with no network, a frozen clock, seeded
  randomness, a fixed locale and timezone and your masked regions, and
  compare screenshot, DOM, accessibility tree (roles, labels, states),
  per-page network and console, layout shift and main-thread time. They run
  on your machine only. Configure them in the trusted base
  `.lemoncrow/review.yaml`:

  ```yaml
  render:
    static_root: public          # or omit to use the web preview's build
    routes: [/, /pricing]        # rendered when the affected routes are unknown
    component_routes:
      "src/components/Nav*": [/, /pricing]
    viewports:
      - {id: desktop, width: 1280, height: 800}
      - {id: mobile, width: 390, height: 844}
    themes: [light, dark]
    mask: [".timestamp"]
    max_cells: 24                # matrix depth
    interactions:
      - id: open-menu
        route: /
        steps: [{click: "#menu"}]
  ```

  Only the routes a change affects are rendered when the mapping is known;
  otherwise the configured routes render and the result says so. The matrix
  stops at `max_cells` and at the entry's budget, and says **Limited by
  budget** (or depth) with what it left out.

Screenshots and DOM/accessibility snapshots are masked and redacted before
they are stored, and follow raw retention: unless a judgment cites them they
expire, and re-running the entry observes them again.

## Signed test results (in-toto)

A CI system that signs its results can post them as typed evidence. The result
is a DSSE envelope over an in-toto Statement with the test-result predicate
(`https://in-toto.io/attestation/test-result/v0.1`):

```bash
curl -X POST "$LEMONCROW/api/reviews/$REVIEW/checks/ingest" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"format": "in_toto", "envelope": '"$(cat result.dsse.json)"', "title": "Unit tests"}'
```

The repository names the keys it trusts in the trusted base's
`.lemoncrow/review.yaml`, as public JWKs (Ed25519 or P-256):

```yaml
attestation_keys:
  - {kid: release-ci, kty: OKP, crv: Ed25519, x: "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}
```

- Only keys on the base branch count. A key added by the Change under review
  verifies nothing for that Review. A key with `status: revoked`, a past
  `revoked_at` or `not_after`, or a future `not_before` verifies nothing.
- The result is refused (422) unless a signature verifies with one of those
  keys. It is never stored as weaker evidence.
- The Statement decides everything that matters: the commit (exactly one
  `gitCommit` subject, which must be the Snapshot's head), the status
  (`PASSED`, `FAILED`, or `WARNED`, which counts as unknown) and the check
  identity (the first `configuration` entry's `uri`, else its `name`). The
  body's `title` is display only.
- The result's integration is the verifying key's RFC 7638 thumbprint, never
  the `kid` or the envelope's `keyid`. A required check or a `ci_equivalents`
  row with `attestation_class: signed_attestation` names `provider: in-toto`
  and that thumbprint as `integration` (`integration_id` in hosted policy), so
  it is met only by that key's results.
- Only the signed content is stored with the result (the payload, its type and
  the one signature that verified), so it can be re-verified later. A signed
  payload is limited to 2 MiB.
- A result is identified by its signed payload and the verifying key. Posting
  the same signed result again, however its envelope, `keyid` or title is
  altered, returns the stored row with `created: false`, so an older result
  can never be replayed over a newer one. The test-result predicate carries no
  signed finish time, so results are ordered by when they were ingested.
- The trusted base is re-read whenever a stored signed result is counted. Once
  it no longer lists the key, or revokes it, that key's results stop counting
  (a key revoked with no `revoked_at` revokes everything it signed). Any revoked
  copy of a key listed twice revokes it.
- Key problems in `attestation_keys` (a malformed or repeated key, more than
  16) are returned in the ingest response's `errors`, and named in a 422.
- A premerge `required-verification` check that accepts `signed_attestation`
  must list `key_thumbprints`, or the policy is refused. A signed result meets
  it only by its signed identity (the `configuration` URI or name, or its full
  check key) and only from a listed key, never by its display title:

  ```yaml
  premerge:
    checks:
      - id: required-verification
        checks: ["https://ci.example.com/jobs/unit"]
        attestation: [signed_attestation]
        key_thumbprints: ["<RFC 7638 thumbprint>"]
  ```

## Data flow

One answer to "for this repository under this policy, where can source, code
and evidence travel?", computed from the effective Review policy. It adds no
policy of its own.

```bash
lc review data-flow                 # trusted base: origin/HEAD, else HEAD
lc review data-flow --base main     # policy committed at another ref
lc review data-flow --json          # the same rows for scripts
```

Policy is read from `.lemoncrow/review.yaml` at the trusted base commit, never
from the working tree.

Hosted, the same projection is `GET /v1/repos/{repo_id}/data-flow`
(`?format=text` for the human text). It needs `review:read` on the repository.
Both surfaces call one builder, so the rows match for the same policy.

- The hosted trusted base is the provider-proven base that a Review captured
  for its Snapshot, never a live read of the default branch. Without
  `?review_id=` the answer reads the most recently updated Review;
  `?review_id=<id>` pins one. The `policy.source_generation` row names the
  Review, its Snapshot, and the captured commit. A Review that is unknown or
  belongs to another repository answers 404.
- A repository that the organization has placed or viewed but never reviewed
  still gets an answer. Operator policy and deployment settings apply, and
  `policy.source_generation` is `not_set` with "no Review-specific source
  generation yet". Asking never creates Review state for that repository.

Each row covers one place data can go:

- source storage and region;
- intelligence routes and the models they allow;
- each route's model-data locus (`models.locus.*`) and its provider training
  and retention terms (`models.terms.*`);
- the models a Snapshot's intelligence runs recorded (`models.recorded`);
- AI content access;
- public web for automatic review and Ask;
- where AI and Runtime checks run (`execution.*`, including
  `execution.locus.<work>` when a component reports its executor, such as a
  customer-operated worker);
- whether reviewed and untrusted code may execute;
- artifact retention;
- deployment and observability feeds;
- network egress, proxy and custom CA settings.

Every row names its origin: the setting and the policy layer it came from
(`built_in_default`, `organization`, `repository`,
`trusted_base_repository`, `environment`, `deployment_environment`, ...).

Statuses are `allowed`, `restricted`, `denied`, `off`, `set`, `not_set`,
`unknown` and `fact`. `not_declared` remains in the vocabulary for older
readers but is no longer emitted. A denial from any layer wins over an allow
from another.

Nothing is left out or assumed allowed. Each of these shows `unknown` with
the reason:

- a missing or provisional trusted base;
- an unrecognized route;
- a setting with no producer;
- any provider term, region or egress state that the operator has not
  attested.

A missing or provisional trusted base also denies model access. The
repository's own restrictions on model access cannot be read, so an
`ai_denied` rule for `**` (origin "unreadable trusted-base repository Review
policy") covers every path, and the intelligence route, Ask-model and
execution-evidence rows are `denied`. Organization content-access rules still
apply. Hosted, no path of a Snapshot without a provider-proven trusted base
(a client capture, or a provider base that could not be read) reaches a
model, and no automatic AI job is queued for it. Locally, a trusted base
that does not resolve or cannot be read denies model access the same way.

Rows never print secrets or sensitive endpoints:

- Proxy and CA rows name the environment settings that are set, never their
  values.
- A custom endpoint row shows the endpoint's host, without scheme,
  credentials, port or path. The host is shown on purpose: it says where
  content goes.
- A configured model id is shown only when it has the shape
  `[vendor/]name[:tag]`: a vendor prefix from a fixed list (`anthropic`,
  `openai`, `ollama`, `bedrock`, ...), a name whose only dots sit between two
  digits (`gpt-3.5`), and a tag with a letter (never a port). An id with a
  host, address, URL, `@`, or credential shape is withheld ("model id
  withheld"), and so are some unusual real ids, such as Bedrock's
  `anthropic.claude-...-v1:0`.
- Customer worker ids, route ids, custom endpoint names and observability
  connector ids are shown only when they are plain names: letters, digits,
  `-` and `_`, at least one letter, and no credential shape. Otherwise the
  row uses the entry's position ("Custom endpoint 2 (name withheld)"), and so
  do its id and origin.
- A credential shape is anything the shared secret scrubbers match, a secret
  word such as `password` or `secret` (also in leetspeak), or a
  letter-and-digit run that is 24 or more characters long, switches between
  letters and digits four or more times, or is 12 or more characters long and
  switches three times. A short secret made of plain words can still pass,
  so do not put secrets in names.
- A settings file that cannot be read is named without its error text, which
  may quote the file.
- A provider error recorded with a captured trusted base is never echoed.

### Organization execution block

The hosted operator policy can switch off model calls or code execution for
the whole organization. Only the organization layer (`defaults`) may set it:

```json
{"version": 1, "defaults": {"execution_block": {"ai": true, "execution": true}}}
```

- The block is above every repository, trusted-base and personal setting,
  and nothing below it can lift it.
- `ai` turns off automatic AI review, Ask and every intelligence capability.
- `execution` turns off Runtime checks, reviewed-code execution and
  customer-worker production observation (queued and running observation
  jobs are superseded). It also covers a required LemonCrow-run check that
  no executor maps (planned as `check:<key>`); such a check never runs
  anyway. Static analysis only reads content the Review already holds, so
  neither kind stops it.
- A malformed block blocks both.
- In the data-flow answer, `policy.organization_block` is `denied` and names
  the block. The intelligence, route, Ask-model and reviewed-code rows it
  covers are `denied`, and run plan entries say "blocked by organization
  policy (execution_block.ai)" (or `.execution`).
- The hosted organization settings (`GET /v1/organization/review-policy`)
  show it as `organization_block`: each kind, the reason it blocks, and its
  origin. It is read-only there; the operator Review policy sets it.

### Operator attestations

LemonCrow cannot observe:

- where a model provider keeps prompts or whether it trains on them;
- which region holds Review data;
- whether the deployment can reach the internet.

The operator attests these in one strict JSON document named by
`LEMONCROW_DATA_GOVERNANCE_FILE`:

```json
{
  "version": 1,
  "storage": {"region": "eu-west-1"},
  "network": {"egress": "proxy_only"},
  "model_routes": {
    "litellm": {"trust": "customer_local", "provider": "acme-vllm", "data_region": "eu-west-1",
                "training": "no_training", "retention_days": 0},
    "claude": {"training": "no_training", "retention_days": 30}
  }
}
```

| Key | Values |
| --- | --- |
| `storage.region` | A region label. |
| `network.egress` | `no_egress` (`denied`), `proxy_only` (`restricted`) or `unrestricted` (`allowed`). This is an attestation; LemonCrow does not enforce it. |
| `model_routes.<driver>.trust` | `customer_local` or `external`. Only `litellm`, `openai` and `ollama` may attest it. Agent CLI routes (`claude`, `codex`, `opencode`) are always `external`. |
| `model_routes.<driver>.provider` / `.data_region` | Labels. |
| `model_routes.<driver>.training` | `no_training` or `may_train`. |
| `model_routes.<driver>.retention_days` | `0` to `36500`. |

Rules:

- With `LEMONCROW_DATA_GOVERNANCE_FILE` unset nothing is attested, and terms,
  regions and egress are `unknown`. Route trust then comes from the driver
  alone: `claude`, `codex`, `opencode` and `openai` are `external`, and
  `ollama`, `litellm` and any other driver are `unknown`. A driver name never
  makes a route `customer_local`.
- To use a local Ollama for paths marked `customer_local_only`, attest it:
  `{"version": 1, "model_routes": {"ollama": {"trust": "customer_local"}}}`.
  The same rule holds locally and hosted.
- Labels are lowercase slugs of at most 48 characters. A label shaped like a
  host, URL or credential is refused.
- An attested route trust is what AI content access enforces. Ask, AI review,
  rule checks and hosted managed jobs read the same value the `models.locus.*`
  row shows, so a route attested `external` is denied paths marked
  `customer_local_only`.
- Nothing is inferred from a hostname, an endpoint URL or a provider's name.
- An attestation binds to the driver route, not to an endpoint URL. Keeping
  the configured endpoint consistent with the attestation is the operator's
  responsibility.
- The document is all-or-nothing. A document that is missing, larger than
  64 KiB, not UTF-8, unparsable, or that holds a duplicate or unknown key or
  value attests nothing. Every route whose trust it could change then becomes
  `unknown`, so it is denied paths marked `customer_local_only` (fail closed).
- Neither the document's path nor its error text is echoed.

Each process reads the document itself. Set the same document in every
process that enforces it:

- the local CLI and its loopback Review server;
- the hosted API server, which answers the data-flow endpoint and plans
  durable Review Intelligence jobs;
- every managed Review job process: `python -m
  lemoncrow_server.managed_review_jobs --execution-class review_intelligence`
  (calls the model) and `--execution-class publication` (publishes results).

A durable Review Intelligence job binds the digest of the attestations the
API server read when it planned the job. The digest covers what the document
attests, not its path or formatting. A managed worker that reads a different
document, or none, cancels the job as stale authority before any model call,
and a publication process that reads a different document marks the result
`superseded`. The next job planned under the same document runs. Customer-operated workers
do not read this document: a customer worker route's trust is its
`data_locus` in the operator's customer worker routes file.

## Diagnostic bundle

One redacted export for support: versions, the protocol window and Review
capability manifest, the managed worker's protocol compatibility, data-flow
origins, where review work runs and where model data goes, Gate I and Gate H
state, Snapshot ids, AI and queued job states, provider operation and webhook
delivery ids, degradation reasons and recent errors.

```bash
lc review diagnostics                         # print the bundle (JSON)
lc review diagnostics --out bundle.json       # write it owner-only (0600)
lc review diagnostics --include error-text    # add one named extra item
lc review diagnostics --repository-id repo-a  # read one repository's store only
```

Locally it reads the loopback server's Review stores read-only
(`<root>/server/data/reviews`, or `--review-root`), this client's protocol
window, the configured server's discovery document and the data-flow origins
of the current repository. Hosted, an organization admin gets the same bundle
for their own organization from `GET /v1/admin/reviews/diagnostic-bundle`
(`?repo_id=` narrows it to one of the organization's repositories). Both
surfaces call one builder, so section names and the schema version match
(`schema_version` 2).

Repositories are never cut by id order. An explicit repository id
(`--repository-id`, `?repo_id=`) reads that repository alone. Otherwise the
bundle reads the repositories whose Review was most recently updated, by the
durable `updated_at` the Review store records (not file times), and hosted
ranks only the repositories the caller may read: a repository the caller
cannot read is never scanned, never ranked and never named, even by id.

The bundle carries no source and no secrets:

- Records keep only allowlisted fields (ids, states, kinds, counts and
  times). An id or state shaped like a key or token is replaced by
  `<withheld>` or `other`. Titles, paths, packets, prompts, model output,
  diff hunks and comment bodies are never read.
- Error messages and degradation reasons go through one redaction pass that
  keeps only diagnostic vocabulary, numbers of up to three digits and
  punctuation; everything else becomes `…`. Each is at most 160 characters.
  A fingerprint, keyed per bundle, groups identical messages.
- A section that cannot be collected says `unavailable` with a reason code;
  a state no component reports yet says `unknown` with a reason code. No
  section is ever left out.
- Secrets are matched after decoding `%XX` and `\uXXXX` escapes and folding
  case, width, whitespace, zero-width characters, accents and combining
  marks, and common Cyrillic, Greek and Latin lookalikes, so a value split
  across lines or fields, overlapping another, written full-width, accented
  or disguised with lookalike letters is still removed. Cookie headers are
  removed too. The secrets are: the value of every secret-named environment
  variable of at least 5 characters (a shorter value is a word or a flag, not
  a secret); the password and the secret-named query values (`token`, `key`,
  `sig`, ...) of URLs in any environment value, not the user name or
  `sslmode=require`; the `api_key` values kept in `providers.json` (local);
  and, hosted, the server's OIDC client secret and demo token and the contents
  of the secret files it reads (token registry, GitHub webhook secret and
  token, notification webhook secret, entitlement tokens). The value of a
  variable whose name is not secret-like (`LEMONCROW_LLM_BACKEND=anthropic`,
  `verify-full`, `en_US.UTF-8`) is not a secret and stays readable. A model
  id or endpoint name whose last segment equals, contains, sits inside or
  shares a run of 6 characters with a secret is shown as `<withheld>`; the
  setting vocabulary in front of the name is never tested. The always-on
  `message` and degradation reasons have the same secrets masked before the
  vocabulary pass, so a secret made of vocabulary words does not survive it.
- Hosted output is read by a tenant who also controls some of the text, so the
  guard answers only for whole values and for overlaps of 8 or more characters:
  it does no fragment or cut-prefix matching, and a short guess is never marked
  differently from random text. Locally the reader owns the environment and the
  guard is thorough. Known limits: a secret written in chunks of 5 characters
  or fewer, or with punctuation between its characters, is not matched, and a
  secret kept only in a vault is not known.

New sections: `execution_locus` (the executor kinds and model route trust the
data-flow projection reports; one locus model, not a second), `gates` (Gate I
and Gate H mode and threshold approval, Gate I's baseline decision and date;
a Gate H contract (the packaged `gate_h_contract.yaml`, present in every
install) that is missing or invalid, where the runtime falls back to built-in
defaults, is `unknown`, not a configured report mode; Gate I's configuration is
a source-checkout file, so an installed package reports Gate I `unknown`; Gate H
is read fresh on every bundle; and `release`, which is `unknown` with a reason until
release qualification reports), and a `protocol.notes` entry
`no_managed_worker` or `worker_unreachable` when the managed worker's
compatibility could not be read. `job_lifecycle` and `background_budget` come
from the hosted PostgreSQL job queue's tenant view, for the caller's
organization only: queued, retry-backoff, oldest and p95 wait per execution
class, jobs waiting on each fair-share or ceiling reason, running model and
deep-review work, the retention horizon in days, the fair shares and ceilings,
the foreground headroom and whether background work leaves it free. Retired
jobs are counted per execution class and in total from the retirement fold,
which keeps them for `retired_jobs_window_days` (400); `retired_jobs` says
`reported`, or `unreadable` when that read fails (no count is shown, never
zero). With any other job queue, or when the read fails, both sections say
`unknown` with a reason.

`--include` adds exactly the named item and nothing else, and the bundle
records what was requested, by whom and when. Unknown names are refused.

| Include | Adds |
| --- | --- |
| `error-text` | Each recent error's message, up to 1,000 characters, with environment values, URL credentials and query strings, bearer and API-key shapes (full-width spellings of each too), long letter-and-digit runs, e-mail addresses and local paths removed. At most 4,000 characters are read; a value or token that bound would split is removed whole. |
| `model-names` | The model id each AI job ran with; an id shaped like a credential, or equal to, containing or inside a configured secret value, is withheld. |

Hosted, both includes are off by default and are granted only to a caller
holding the `diagnostics:read` permission, which the default `admin` role does
not hold: an operator grants it by binding the `support` role (a group binding
in the RBAC bindings file, or a token's roles) alongside `admin`. A caller
without it still gets the default bundle; asking for an include is refused.
Hosted error text passes the same redaction as local, drops the name of the
environment variable it removed, and is bounded. Every hosted export is
security-audited (`review.diagnostic_bundle`).

Hosted bundle requests spend their own `support_rate` quota (by default 20 per
organization and 10 per user per ten minutes), never the request rate or the
model-spend budget, so a bundle can be exported while either is exhausted. A
refusal is `quota_support_rate` and is retryable.

Bounds: the 20 most recently updated Reviews across at most 25 repository
stores, at most 50 AI jobs, 50 queued jobs, 50 provider operations, 20 webhook
deliveries, 20 degraded Snapshots (16 reasons each), 25 recent errors and the
data flow of 3 repositories. Each list says whether it was truncated, and the
bundle states its limits.

## Production observation on a customer worker

After a merged change reaches production, Review compares what it predicted
(changed routes, removed outbound hosts) with production telemetry. On a hosted
deployment the hosted service never queries telemetry and never holds a
telemetry credential. A customer worker on your own infrastructure runs the
queries and reports bounded aggregates only: counts, latency quantiles and
error-group fingerprints.

It stays off until all of these are in place:

1. Hosted Review policy configures `observability` connectors. Each connector
   names its endpoint and the *name* of the environment variable that holds
   its credential, never the value.
2. The operator designates the worker in the customer worker routes file with
   `"production_observation": true` on its `{"org_id", "worker_id"}` entry. A
   worker that is not designated is refused with `403` on every observation
   call: claim, result, fail, the dry run and the plan routes. Revoking the
   designation cancels a running lease at its next heartbeat. The routes file
   is read on every call, so a change takes effect at once.
3. The worker host pins the connectors it may call:

   ```json
   {"version": 1, "connectors": [
     {"kind": "prometheus", "base_url": "https://prom.internal.example", "credential_env": "PROM_TOKEN"},
     {"kind": "sentry", "base_url": "https://sentry.internal.example", "credential_env": "SENTRY_TOKEN"}
   ]}
   ```

   The worker sends a credential only to a connector whose kind, endpoint and
   variable name all match a pin, so a changed hosted policy cannot redirect
   it. Queries for a connector without a pin stay unanswered. A lease with no
   pinned connector is dead-lettered.

The hosted deployment also needs its durable job queue (PostgreSQL, metadata
schema 10; upgrade every replica before designating a worker, see
[production notes](../operations/production-readiness.md#metadata-schema-10-production-observation))
and its worker envelope signing key, as for any customer worker.

The effective execution policy admits observation for a repository only when
its run settings resolve without errors, it allows the `worker` locus for
this execution class (`allowed_run_on_by_class.default`, else
`allowed_run_on`) and it does not mark that locus unavailable
(`unavailable_run_on_by_class.default.worker`). Effort and budget changes do
not affect admission.

```bash
export LEMONCROW_WORKER_TOKEN=...   # the worker identity's token (--token-env names another variable)
lc review observation-worker check --server https://lemoncrow.example.com --pins pins.json
lc review observation-worker run --server https://lemoncrow.example.com --pins pins.json
```

`check` is a dry run. It shows whether the hosted service is reachable, this
worker is designated, each repository's execution policy admits observation
and each pinned connector accepts its credential. Its requests read no
telemetry: Prometheus evaluates the constant `vector(1)` and Sentry returns
project metadata, which is discarded. It exits `1` when anything is not
ready. `run` polls every 60 seconds (`--interval`, at least 5). With `--once`
it drains the due work and exits.

Each due observation window becomes one leased job on the hosted job queue.
The signed lease carries the planned query ids; the claim response carries
the queries themselves, so the lease stays small (about 16 KB) even at the
largest query budget of 200 queries. The hosted service checks the job when
a worker claims it, on every heartbeat and before it publishes the result.
It cancels the job when production rolled the change back, the Review's merge
moved to another commit, the window closed, the observability policy changed
or the effective execution policy no longer admits it. A report from a
cancelled lease is refused with `409`; one that arrives before the
cancellation is stored but marked superseded, never published.

A timeout, a connection failure or an HTTP error retries the whole plan after
1 minute, then 2 (doubling, at most 30), for up to 3 attempts. The last attempt
reports what it has. An unauthorized credential is recorded as that
connector's answer and is not retried. The worker retries a failed heartbeat
twice (2 and 4 seconds apart). When the hosted service stays unreachable, or
a report fails with a server error, the worker gives the lease back with a
transient code and the queue retries it after its backoff. `run` keeps
polling after a failed pass, doubling its wait up to 15 minutes.

A window whose job ends without an answer (cancelled, out of attempts, or
dead-lettered) is offered again as a new job after a hold: 5 minutes after
the first offer, then 10 and 20, at most 60. A dead-lettered offer (a code
the worker cannot retry, such as `connector_not_pinned`) waits at least 60
minutes. A window is offered at most 4 times. It is never offered while the
reason it was cancelled still holds, such as a rollback or a refused
admission. After the fourth offer the window is left to close, and its
unanswered predictions end `not_observed`. A recorded answer or a policy
change makes a new window version, which starts a new count.

A report from an expired lease is still accepted if the queue has not yet
reclaimed the lease and the window is still current. After the reclaim it is
refused with `409`. A repeated report records each answer once.
When a window closes, its summary is published only by the closer that wins
the window's compare-and-set. A window whose close fails is logged, counted
and retried on the next pass; the other windows still close.

The unleased plan routes (`/v1/review-workers/observations` and
`/v1/review-workers/observations/aggregates`) apply the same deployment
check. They do not plan a rolled-back or superseded deployment, and they
refuse its aggregates with `409`.

Not supported yet: OTLP or trace ingestion (OpenTelemetry metrics work only
through a Prometheus-compatible query endpoint), other telemetry vendors, and
running connectors on the hosted service itself.

## Where to go next

- Run the [ten-minute walkthrough](review-walkthrough.md) on a scratch repo.
- Look up a specific flag or output field in the
  [CLI reference](cli.md#reviewing-agent-written-changes).
- Add previews for another platform (Storybook is built in) with
  [Review preview plugins](review-preview-plugins.md).
- Hit something unexpected? The walkthrough's
  [troubleshooting table](review-walkthrough.md#troubleshooting) covers the
  common cases (no index, no session recorded, clean tree, orphaned comments).
