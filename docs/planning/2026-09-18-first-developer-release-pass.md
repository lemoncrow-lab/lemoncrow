# First developer release pass

Date: 2026-09-18

Baseline: combined `main` after the single-server, durable Review, Critique-style reader, revision-link, and provider integration work.

## Release story

The first developer release is judged against one loop:

1. Developer A runs `lc review` on a branch or working tree.
2. Developer B opens one stable Review URL, reads the diff, leaves comments, and records target judgments.
3. Developer A changes the code and uploads again.
4. The same Review ID receives a new immutable revision.
5. Developer B returns to the same URL and sees valid prior work preserved, changed targets reopened, comments retained or conservatively orphaned, and only current work requiring attention.
6. The reviewer can finish with an explicit summary and, when provider-linked, deliberately publish selected human comments/outcomes to the Git host.

## Result

The implemented release-critical path is green in automated verification.

### Stable Review identity and revision refresh — pass

- Repeated working-tree Review capture keeps the same Review ID.
- Changed content creates a new revision ID and increments the revision number.
- Local and hosted modes use the configured LemonCrow server; Review does not start a per-repository listener.
- Hosted browser URLs never embed the CLI bearer token.
- Local browser bootstrap uses the loopback fragment token.

A dedicated CLI regression now pins the exact `rev 1 -> edit -> same review id -> rev 2` developer loop.

### Diff reading and navigation — pass

The Reader tests cover:

- continuous multi-file diff reading;
- split/unified switching;
- target/file keyboard navigation;
- search and zero-match recovery;
- large-review streaming by changed-line budget;
- jumping directly to a distant search result without loading every intervening file;
- source-first loading while enrichment continues;
- file/target collapse and review state controls.

The production frontend build and TypeScript check pass.

### Durable human state and re-review — pass

Verified behavior includes:

- reviewed / needs-changes / unreviewed / changed-since-review states;
- content-bound reconciliation;
- unchanged judgments carried forward;
- changed targets reopened;
- independent reviewer frontiers;
- stale network responses prevented from replacing newer revision state;
- draft comment text preserved and detached for human re-anchoring when its source changes;
- author responses surfaced even when source bytes do not change.

### Comments and correction handoff — pass

Verified behavior includes:

- line/range comments and request-change threads;
- replies and reviewer/author turn ownership;
- resolve/reopen lifecycle;
- annotation version history across revisions;
- conservative relocation/orphaning;
- feedback preview bound to exact revision, feedback hash, annotation versions, and operation ID;
- idempotent delivery semantics;
- blocked/failed/uncertain delivery states;
- copy/export fallback.

This pass fixed one user-facing failure-recovery gap: clipboard copy now reports success, and a failed/unavailable clipboard tells the reviewer to use the visible preview for manual copy instead of failing silently.

### Verification and Finish Review — pass

Current verification distinguishes pass, fail, not-run, and unknown. Evidence is revision-bound; previous-revision artifacts are shown as history rather than current proof.

Finish Review exposes:

- unreviewed targets;
- changed-since-review targets;
- needs-changes targets;
- unknown target identity;
- open/orphaned human comments;
- failed verification;
- unresolved verification;
- previous-revision evidence;
- discarded verdict history.

An overall verdict is explicit and separate from both LemonCrow lifecycle state and provider approval.

### Sharing, auth, and provider publication — pass for first cohort

Hosted authorization and Reader bridging pass the shipped route tests. Provider-linked reviews can explicitly publish a Git-host outcome and selected human comments; GitHub webhook ingestion and publication are covered by hosted/provider tests.

## Verification run

The release pass ran the following focused suites successfully:

- frontend Review UI: 117 tests;
- hosted Review + provider + GitHub: 59 tests;
- hosted authorization / Reader bridge: 25 tests;
- Review API: 89 tests;
- evidence / impact / incremental impact: 68 tests;
- feedback delivery / correction loop: 13 tests;
- attention / ordering: 29 tests;
- target reconciliation: 26 tests;
- Review understanding end-to-end: 19 tests;
- Review store / migrations / persistence: 78 tests;
- full Review CLI suite: 105 tests;
- frontend production build: pass;
- frontend TypeScript check: pass.

The CLI suite was run with four pytest workers to fit the interactive orchestration window; all 105 tests passed.

## Not release blockers

Do not expand scope before the first cohort for:

- merge controls;
- approval rules / CODEOWNERS policy authoring;
- notifications;
- stacks;
- guest review;
- arbitrary revision A/B comparison beyond the current history surface;
- executable suggestions;
- VS Code integration;
- analytics;
- additional SCM providers.

## Known pilot follow-ups

These should be observed in real developer use rather than implemented preemptively.

1. **Manual GitHub PR import entry point.** GitHub ingestion is currently provider/webhook driven. There is no polished “paste PR URL / `lc review --pr`” developer action. This does not block the branch-based first-cohort loop, but it is a likely usability request if GitHub-first onboarding is expected.
2. **Live direct-agent compatibility smoke.** Mocked and contract tests cover exact-session delivery, stale preview rejection, idempotency, and uncertain outcomes. A disposable live Claude/Codex session should still be exercised when an eligible session is available.
3. **Browser-perceived startup latency.** Source-first loading, lazy preparation, and large-review streaming are covered, but this pass did not establish a real-browser latency baseline. Measure before optimizing; do not infer a blocker from bundle size alone.
4. **Real two-account hosted dogfood.** Authorization and independent-reviewer behavior are covered in server tests, but a real browser session with two authenticated developer accounts remains useful pilot validation.

## Release decision

No release-critical product defect was found after the clipboard recovery fix.

The combined repo is ready for a small developer cohort focused on the branch-based Review loop. The next work should come from observed friction in those first sessions, not from adding platform-parity features preemptively.
