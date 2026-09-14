# LemonCrow Review — dogfood completion checklist

Status: **feature freeze after R18**. Do not resume R19–R24 until this checklist is satisfied by reviewing `origin/main...feat/review-usage-model` in LemonCrow itself.

Acceptance target:

> A human can understand, inspect, annotate, verify, and complete the full branch review without needing the implementation author to explain the UI and without routine escapes to GitHub, an IDE, or another chat merely to reconstruct context.

## P0 — make the self-review truthful and current

- [x] **D1 — Fresh workspace generation.** `lc review --open` must never adopt a healthy process serving older review backend/frontend code. Registration carries a generation fingerprint; mismatches replace the old workspace automatically.
- [x] **D2 — Structured verification evidence.** Current test/build/lint/migration evidence can carry `PASS | FAIL | NOT_RUN | UNKNOWN` plus detail and supersedes an older packet observation with the same check name.
- [x] **D3 — Automatic deterministic preparation.** Opening/tracking a revision creates a small, idempotent LemonCrow annotation layer and a compact Review Brief from already-observed facts; no AI verdicts.
- [x] **D4 — Author-side evidence capture.** Exact author sessions can record screenshots, video, traces, documents, preview captures, and verification results without advancing the human ReviewRevision; import is revision-bound and fail-closed.
- [ ] **D5 — Populate the real branch review.** The durable `origin/main...feat/review-usage-model` session contains real focused/full-suite/typecheck/lint/migration evidence, deterministic annotations, and at least one current screenshot of the actual review workspace. Do not fabricate green checks or visual proof.

## P0 — make the populated review understandable

- [x] **D6 — Review Brief answers orientation immediately.** Before opening a file, the reviewer can see what the branch changes, the meaningful themes/chapters, what deserves attention first, verification state, annotation coverage, and current/stale proof coverage.
- [x] **D7 — Right-side Evidence Desk is decision-oriented.** Resizable/persistent panel; Summary → Evidence → Discussion; no raw data dump; stale proof unmistakable; HUMAN / AUTHOR / LEMONCROW / AI REVIEW remain visually and semantically distinct.
- [x] **D8 — File context is useful enough to judge.** For a selected file, surface why it matters, semantic impact/callers, definitions/contracts, relevant verification, provenance, rationale/annotations, and visual evidence without scavenger hunting.
- [x] **D9 — Evidence is connected, not decorative.** Verification artifacts affect the verification summary; screenshots/traces/video show which revision/file they prove; older evidence becomes stale automatically; silence never becomes PASS.
- [x] **D10 — Large-review navigation works.** Attention, Intent, Dependency, and Commits lenses produce comprehensible chunks for the current 192-file branch; broad scopes are split into human-sized chapters, utility buckets stay compressed, and the reviewer always knows what to inspect next.
- [x] **D10a — Named branch review continuity.** A stable named range such as `origin/main...feat/review-usage-model` remains one ReviewSession as branch heads advance; new commits become ReviewRevisions, relative/SHA ranges stay snapshot-isolated, and compatible legacy SHA-keyed sessions preserve human review state instead of creating another empty session.

## P0 — complete the actual dogfood review

- [x] **D11 — Screenshot acceptance pass.** Capture the populated workspace at desktop width and inspect it as a product artifact. Fix obvious hierarchy, density, truncation, empty-state, and evidence-discovery failures before asking the user to review.
- [ ] **D12 — Human self-review pass.** Review all meaningful changes from `origin/main` through the current branch using LemonCrow itself. Every `I do not understand this`, `why is this here?`, `where do I click?`, `I need the IDE/GitHub/chat for this`, or `I lost my place` is recorded as a product/UX bug.
- [x] **D12a — Chapter close-out without bulk approval.** After a chapter has zero attention/changed items, the human can explicitly mark its ordinary unreviewed remainder reviewed in one action. The server rechecks the current revision and skips attention, changed, needs-changes, mechanical, unknown, and already-reviewed files rather than trusting stale browser state.
- [ ] **D13 — Revision loop pass.** During that self-review, create human comments, send feedback to an exact Claude session where provenance permits, receive/edit a new revision, and prove only changed work reopens while prior understanding remains preserved.
- [x] **D14 — Completion confidence.** Finish Review is meaningful: unresolved human judgment, stale evidence, failed/unknown verification, changed-since-review, and discarded verdicts are visible rather than silently hidden. Previous-revision proof is shown as history rather than falsely counted as unfinished work.

## Quality gates for every dogfood fix

- [x] Targeted backend tests pass.
- [x] Frontend review tests + TypeScript pass when UI/API changes.
- [x] Production frontend build passes when the review workspace changes.
- [x] `git diff --check` passes.
- [x] Existing unrelated working-tree edits are preserved.
- [x] No new feature is justified only by making the interface look more populated.

## Explicitly postponed until this checklist passes

- R19 Review Skills / AI hints
- R20 Ask This Change / Ask This Hunk
- R21 GitHub + stacked-PR read adapter
- R22 GitHub publishing
- R24 Personal Review Inbox

R23 is split: the **human-facing evidence substrate** is part of this dogfood gate; broader environment orchestration and automatic video generation remain postponed.
