# AI-native Review — confidence without cognitive load

Status: ACTIVE
Worktree: `review-agent-handoff`
Branch: `feat/review-agent-handoff`

## Product outcome

A developer should be able to understand what changed, know what has actually been verified, see exactly what still needs human judgment, and take the next action without reconstructing review state themselves.

The Review loop becomes:

```text
Understand the change
  -> follow the attention path
  -> make human judgments
  -> send corrections to the exact coding agent
  -> agent fixes + verifies
  -> re-review only returned/changed work
  -> finish with explicit evidence and unknowns
```

The product principle is **confidence without cognitive load**.

Every primary Review feature must do at least one of these:

1. save the developer from reconstructing information; or
2. provide inspectable evidence for a decision.

If it does neither, it does not belong on the primary Review surface.

## Non-negotiable design rules

These extend the Review polish freeze rather than replace it.

- The diff still owns the screen.
- No generic AI summary panel and no always-open PR chat.
- No opaque confidence/risk percentage.
- Human judgment remains stronger than machine claims.
- `sent` != fixed; `addressed` != resolved; test presence != test pass.
- Existing durable `ReviewTarget`, revision, annotation, evidence, and delivery state remain authoritative.
- Story/attention/navigation are projections over authoritative state, never another state machine.
- Unknown/stale/ambiguous evidence stays visible as unknown/stale/ambiguous.
- One obvious next action should follow from durable state.

## Experience model

### 1. Change story — "what changed?"

Use deterministic chapter data already produced by Review. Present a short conceptual path such as:

```text
Identity -> API -> Settings UI
```

This is orientation, not generated prose. Each step links into the relevant source area. Utility groups such as tests/docs stay secondary unless they are the only meaningful groups.

### 2. Attention path — "where should I look first?"

Use existing attention rank/reasons and `brief.review_first`. The UI should expose the first useful place and why, then let the existing outline/diff carry the reviewer forward.

Do not invent a second priority score.

### 3. Human state — "what still needs me?"

The durable target/comment state answers this:

- unreviewed/unknown target;
- changed since my judgment;
- human feedback not sent;
- delivery pending;
- waiting on agent;
- addressed -> re-review;
- ready to finish.

One primary action represents the current state transition.

### 4. Evidence — "why should I trust this?"

Confidence is inspectable evidence, not a number:

- checks PASS / FAIL / NOT_RUN / UNKNOWN;
- current vs stale artifacts;
- rendered Web/API/Markdown behavior;
- impact/caller evidence;
- exact authoring-agent provenance;
- explicit limits/degraded analysis.

### 5. Re-review — "what changed since I last looked?"

Preserve valid judgments. Reopen only changed targets. Human follow-up replies create a new correction round. Agent-addressed comments remain open until the human resolves them.

## Delivery phases

### Phase 1 — Compact orientation in the Reader [COMPLETE]

Goal: answer "what is this change, and where do I start?" without opening another sheet.

Tasks:

- [x] Add a compact change-story projection to the Review outline.
- [x] Make story steps navigable to their first changed path.
- [x] Add one compact `Review first` attention entry with the concrete reason.
- [x] Preserve search behavior and state-grouped file navigation.
- [x] Hide orientation when it carries no useful information.
- [x] Keep sidebar width and diff area unchanged.
- [x] Add focused component and Reader integration tests.

Acceptance:

- A multi-area review shows a short deterministic story before file navigation.
- The reviewer can jump from a story step or `Review first` directly into the diff.
- A small/single-area review does not gain decorative chrome.
- Search and large-review behavior do not regress.

### Phase 2 — Attention path at target granularity [COMPLETE]

Goal: move from "priority file" to "next human judgment" while keeping source order comprehensible.

Tasks:

- [x] Derive a compact attention path from `ReviewTarget` state + attention rank.
- [x] Prefer changed-since-review and explicit human blockers over ordinary unreviewed work.
- [x] Expose why each elevated target is next using existing reasons/evidence.
- [x] Add next/previous attention navigation without replacing continuous reading.
- [x] Keep mechanical/generated work collapsed from the attention path while still accessible.

Acceptance:

- The reviewer can answer "what needs my brain next?" without scanning all files.
- No target disappears from the durable review denominator.

### Phase 3 — Evidence-backed confidence summary [COMPLETE]

Goal: answer "what do we actually know?" without a confidence score.

Tasks:

- [x] Reshape readiness around `Verified`, `Needs verification`, `Stale`, and `Unknown` facts.
- [x] Link verification facts to named checks/artifacts through their recorded source.
- [x] Separate author/agent claims from observed evidence.
- [x] Keep failure/not-run/unknown explicit at finish time.
- [x] Avoid duplicating aggregate verification blockers beside the named evidence list.

Acceptance:

- A reviewer can state why a change is ready or not ready from inspectable evidence.
- Missing evidence never reads as success.

### Phase 4 — Unified next-action state machine [COMPLETE]

Goal: one obvious action, derived from durable Review state.

States:

```text
Review next target
Send N comments to <agent>
Delivery pending
Waiting on <agent>
Re-review N
Review changed targets
Ready to finish
```

Tasks:

- [x] Make unsent feedback the primary action.
- [x] Distinguish sent, delivery-pending, addressed, and resolved.
- [x] Preserve human resolution ownership.
- [x] Make safe failed delivery retryable and ambiguous delivery non-resendable.
- [x] Incorporate target-level `Review next` into the same action model after Phase 2.
- [x] Ensure keyboard command palette mirrors the exact same transition.
- [x] Promote source drift, stranded-reader recovery, and verification blockers into the same primary-action model.
- [x] Keep explicit early finish available only as a secondary action while work remains.

### Phase 5 — Correction/re-review integrity [COMPLETE]

Tasks:

- [x] Exact prepared feedback identity and delivery state.
- [x] Reviewer follow-up reply advances the root thread version.
- [x] Nested human replies remain in the next feedback bundle.
- [x] Reviewer follow-up clears stale `addressed` and creates a new send round.
- [x] Agent `addressed` cannot resolve a human comment.
- [x] Add a compact "what returned" orientation after revision refresh using target delta.
- [x] Distinguish newly returned changed/new work from work that was already pending.
- [x] Keep revision-delta explanation secondary to the global primary action.

### Phase 6 — Hosted attention inbox projection [COMPLETE]

Project the same state rather than inventing inbox-specific workflow:

```text
Needs my review
Returned to me
Waiting on agent
Waiting on human author
Changed since my review
Delivery needs inspection
Ready to finish
```

Tasks:

- [x] Reuse the existing durable hosted Review Inbox projection instead of introducing inbox-only state.
- [x] Keep human-author waiting distinct for hosted reviews with a real human author.
- [x] Add `waiting_on_agent` from current-version sent agent-session delivery records.
- [x] Add `delivery_needs_inspection` for dispatching/uncertain or remotely-started failed delivery attempts.
- [x] Make ambiguous delivery take precedence over waiting so the reviewer is not encouraged to resend.
- [x] Expose both states as filterable hosted Inbox buckets.
- [x] Preserve returned/changed/assignment/ready projections and their existing semantics.

The hosted Inbox and local Reader now project the same durable Review state while retaining the one distinction the hosted product genuinely needs: a request can wait on either a coding agent or a human author.

### Phase 7 — CLI preparation feedback [COMPLETE]

Goal: `lc review --open` must never look hung while deterministic capture and publication work is running.

Tasks:

- [x] Surface packet stages as they start: diff, source snapshots, impact, provenance, and review ranking.
- [x] Surface server stages: connect, freeze base/current snapshots, and publish the immutable revision.
- [x] Surface post-capture state loading and browser pairing/open.
- [x] Show elapsed time when a stage completes.
- [x] Keep progress on stderr so JSON stdout stays machine-readable.
- [x] Keep `--no-open` terminal/script workflows quiet.
- [x] Preserve the canonical `Review http://…` output and browser URL semantics.

Example:

```text
Preparing review…
  → Reading changed files and diff…
  ✓ Diff captured · 0.1s
  → Loading exact old/new source snapshots…
  ✓ Source snapshots loaded · 0.2s
  → Analyzing symbols, callers, and change impact…
  ✓ Impact analysis complete · 3.8s
  → Correlating the authoring agent session…
  ✓ Author provenance checked · 0.4s
  → Ranking the human review order…
  ✓ Review order ready · 0.1s
  → Connecting to Review server…
  → Freezing base snapshot…
  → Freezing current snapshot…
  → Publishing immutable Review revision…
  → Loading durable review state…
  → Pairing and opening Review Reader…
```

### Phase 8 — Explicit UI revision capture [COMPLETE]

Goal: a reviewer can accept a settled local source change as a new immutable revision of the same Review without returning to the terminal.

> **Invariant:** detect automatically, capture explicitly.

Tasks:

- [x] Bind local source refresh only to a proven same-host checkout.
- [x] Detect source changes from the mutable checkout, not the server scratch snapshot.
- [x] Keep initial capture and later probes on the same source-fingerprint identity.
- [x] Reuse the existing `refresh` / `record_revision` reconciliation for marks, comments, targets, and revision delta.
- [x] Use the normal LemonCrow store root for provenance/evidence correlation during UI refresh.
- [x] Remove hidden auto-refresh: source detection is automatic; revision creation is explicit.
- [x] Show `Updating review…` while the explicit mutation is running.
- [x] Fail closed for hosted/upload-only Reviews with an actionable `run lc review` message.
- [x] Recompose cached local Reader apps when a trusted checkout binding becomes available.
- [x] Prove end to end that editing the real checkout creates revision 2 under the same Review ID.

The primary action remains **Update review** when source changes. Clicking it snapshots the trusted checkout, creates or reuses an immutable `ReviewRevision`, reconciles durable human state, focuses the returned target delta, and keeps the stable Review URL. Hosted and upload-only snapshots cannot invoke local filesystem refresh semantics.

### Phase 9 — Reviewer-authored source proposals [COMPLETE]

Goal: the Review UI is an authoring workspace over mutable source without weakening immutable Review history or annotation trust semantics.

> **Invariant:** comments express judgment; proposals express source changes.

Tasks:

- [x] Keep `Annotation` as discussion/judgment and add a sibling durable `ReviewChangeProposal` model.
- [x] Persist proposals through an additive ReviewStore migration without bumping the base schema version.
- [x] Read the exact selected source from the immutable Review revision on the server.
- [x] Generate the unified patch server-side; the browser never reconstructs source identity or patch context.
- [x] Bind every proposal to an exact base revision, selected range, and whole-file SHA-256.
- [x] Reject stale Review revisions and stale/externally-edited working source rather than fuzzy-applying.
- [x] Apply accepted proposals atomically only to a trusted same-host checkout.
- [x] Keep Review revisions immutable after apply; the result is only a changed working source until **Update review**.
- [x] Record post-apply source fingerprint and link the proposal only to a later ReviewRevision with the exact same fingerprint.
- [x] Project affected returned ReviewTargets from the captured revision so proposal intent remains connected to the implementation.
- [x] Add inline **Suggest edit** with exact reviewed source, editable replacement, optional intent, server patch preview, and explicit apply.
- [x] Add inline **Edit source** as a one-action create+apply path over the same durable proposal machinery.
- [x] Hide direct source mutation unless the server explicitly reports `source_mutation_supported`; hosted/read-only Review may still retain proposal-only semantics.
- [x] Persist proposal status across reload and expose proposed/applied/conflicted state in the Reader.
- [x] Surface captured proposals in the revision delta and navigate to resulting targets.

First-slice constraints are deliberate: proposals currently target the current/new side of a text diff and one contiguous selected range. Multi-file/refactor proposals, proposal rebase, and proposal-specific coding-agent delegation build on this same durable object rather than introducing alternate write paths.

### Phase 10 — Source advancement vs Review judgment + dedicated source comparison [COMPLETE]

Goal: source freshness must never commandeer the human-review workflow, and source comparison must be a first-class full-screen surface rather than a cramped Review-history widget.

> **Invariant:** review actions advance judgment; source actions advance the Review snapshot.

Tasks:

- [x] Remove source refresh from the shared primary Review action state machine.
- [x] Keep Review next/re-review/readiness/finish as the header's primary workflow action even when the working source is newer.
- [x] Move source advancement into a distinct cyan **Working source is ahead of rev N** banner.
- [x] Make **Update to source** explicit and optional; the current frozen revision remains fully reviewable.
- [x] Keep reviewer-applied source edits visibly awaiting capture until the human chooses to update.
- [x] Keep History focused on immutable Review revisions and append-only activity; remove embedded code comparison from the sheet.
- [x] Preserve historical revision routes as immutable, read-only Review views.
- [x] Add a canonical full-screen comparison route: `/r/x/<from>..<to>`.
- [x] Make both source specs editable so the URL itself identifies the comparison rather than the Review that happened to create it.
- [x] Reuse the production Review diff renderer in an explicit read-only comparison mode; no fake comments, verdicts, target state, bulk-review actions, or comment gutters.
- [x] Add typed comparison sources for `rr~<Review revision>`, `git~<Git ref>`, `HEAD`, `worktree`, and `index`.
- [x] Support Review revision → Review revision, Review revision → worktree/index, Git ref → Git ref, and Git ref → worktree/index through one normalized API.
- [x] Compute Review-revision comparisons from persisted source snapshots with Git-base fallback for paths outside each revision's changed-file blob set.
- [x] Keep Git comparisons on LemonCrow's existing Git diff machinery rather than inventing a second diff implementation.
- [x] Add **Compare source** beside **Update to source** so a reviewer can inspect the newer working tree without first creating a new Review revision.
- [x] Link History's **Compare to latest** action into the same `/r/x/...` surface instead of rendering another compare UI.

## Explicit non-goals

- No generic AI review chatbot.
- No autonomous merge/approval policy.
- No opaque confidence score.
- No duplicate risk scoring model.
- No new issue/project tracker.
- No replacement for SCM merge queues/branch protection.
- No large persistent dashboard above the diff.
- No speculative diagrams by default.

## Validation gate

Each phase must preserve:

- target denominator/progress semantics;
- historical Review immutability;
- incremental refresh/judgment reconciliation;
- source-first startup and large-review virtualization;
- keyboard navigation/search;
- human/agent authority separation;
- revision-bound evidence semantics.

Before folding/commit: full `frontend/src/review` test suite, TypeScript typecheck, affected Review backend suites, Ruff, and `git diff --check`.

Current validation:

- Public Review frontend: 32 test files, 307 tests passed.
- Public Review TypeScript and production build: passed.
- Changed public feedback semantics: 5 focused backend regressions passed.
- Public delivery suite: 12 tests passed.
- Public feedback-loop suite: passed.
- Public Review API: 100 tests passed.
- Public ReviewStore: 73 tests passed.
- Reviewer source-proposal edge cases include older-revision refusal and CRLF-preserving apply.
- Real central-local Review server integration: passed.
- Enterprise hosted Inbox frontend: 5 tests passed.
- Enterprise hosted Inbox server projection: 4 targeted cases passed across memory + SQLite backends.
- Enterprise frontend TypeScript: passed.
- Ruff on changed public/enterprise Python: passed.
- `git diff --check`: passed.
