# Google Critique → LemonCrow Review parity

**Status:** research + implementation decisions  
**Date:** 2026-09-17  
**Scope:** Critique UX/workflow/state-keeping ideas that improve LemonCrow Review without turning LemonCrow into a Git host or merge-policy engine.

## Sources

Primary public source:

- *Software Engineering at Google*, Chapter 19: Critique — https://abseil.io/resources/swe-book/html/ch19.html

Useful related public sources:

- Gerrit Attention Set, explicitly influenced by Critique — https://gerrit-review.googlesource.com/Documentation/user-attention-set.html
- Gerrit Review UI — https://gerrit-review.googlesource.com/Documentation/user-review-ui.html
- Gerrit Attention Set design/use cases — https://gerrit.googlesource.com/homepage/+/master/pages/design-docs/attention-set/use-cases.md

Public Critique screenshots in the Google SWE book show:

- compact file diff chrome;
- yellow draft/unresolved review comments;
- gray analyzer findings;
- `Reply (N drafts)` as the review handoff;
- snapshot identity directly adjacent to the file;
- explicit `Mark this file as reviewed` state;
- a dashboard split into `Needs attention`, `Incoming reviews`, and `Outgoing reviews`.

The screenshots are old. The workflow/state ideas are the important part; LemonCrow should not reproduce the visual styling literally.

---

## 1. The actual Critique state machine

Critique's public flow is approximately:

```text
Author workspace
  ↓
Snapshot N
  ├─ diff
  ├─ analyzers
  └─ presubmit/check state
  ↓
Request review
  ↓
Reviewer inspects latest snapshot
  ├─ per-file reviewed state
  ├─ draft comments
  └─ unresolved/resolved intent
  ↓
Publish review atomically
  ├─ summary reply
  ├─ all draft inline comments
  ├─ LGTM / approval state
  └─ attention set changes
  ↓
Author's turn
  ├─ replies / Done / Ack
  ├─ edits code
  └─ uploads Snapshot N+1
  ↓
Reviewer compares snapshots
  ├─ modified files become unreviewed
  ├─ old discussions remain visible
  └─ reviewer sees what actually changed
  ↓
LGTM + required approval + unresolved-comment state
  ↓
Ready to submit
```

The key point is that Critique models **review as turn-based state over immutable snapshots**, not as a stream of unrelated comments.

LemonCrow should preserve that principle while remaining finer-grained than Critique: our durable human state is attached to content-bound ReviewTargets rather than only whole files.

---

## 2. Critique design principles worth preserving

Google explicitly calls out four principles:

1. **Simplicity** — fast load, easy navigation, keyboard support, obvious review state.
2. **Trust** — do not force redundant re-review merely to prove minor feedback was handled.
3. **Generic communication** — prefer normal comments/suggestions over elaborate workflow protocols.
4. **Workflow integration** — link deeply to code search, editing, tests, release state, etc., without turning Review into a universal IDE.

These map unusually well to LemonCrow.

A useful LemonCrow version is:

> Review should remember what the human understood, clearly name whose turn it is, expose changed evidence, and make handing judgment back to the author cheap. It should not become the author's execution orchestrator.

---

## 3. Parity matrix

| Critique capability | Critique behavior | LemonCrow current direction | Decision |
| --- | --- | --- | --- |
| Immutable snapshots | Every uploaded state is a snapshot; any two can be compared | Durable immutable ReviewRevisions already exist | **Keep / expose better** |
| Snapshot-to-snapshot comparison | Arbitrary snapshot pairs; compact per-file snapshot chain | Historical revisions exist, but pair comparison is not first-class | **P0 add** |
| Snapshot chain at file level | Compact timeline, similar snapshots collapsed, important states highlighted | History sheet is whole-review oriented | **P0 adapt** |
| Per-reviewer reviewed state | Latest-snapshot file checkbox; modification clears it | Semantic/hunk/file ReviewTargets with content-aware preservation/reopen | **LC stronger; do not regress to file truth** |
| File completion convenience | One visible reviewed checkbox | Safe file-remainder completion already exists | **Expose as convenience only** |
| Draft inline comments | Drafts are private until publish | Local comments are durable immediately; correction-round publishing is durable | **P1 for hosted multi-user privacy** |
| Atomic publish | One Reply publishes all drafts and summary atomically | Durable correction rounds publish all unpublished human feedback once | **Parity / LC stronger for agents** |
| Prevent duplicate handoff | Published drafts cease being drafts | Version-aware unpublished feedback now prevents repeat sends | **Parity** |
| Unresolved vs resolved | Unresolved means author action is expected | open/resolved + request_change + addressed-needs-rereview | **LC stronger authority split** |
| Done / Ack shortcuts | Author can indicate handled/read | Author may claim `addressed`; reviewer still resolves | **Adapt, do not copy author-resolution authority** |
| Attention Set | Explicit people currently expected to act | Reader has derived `Your turn / Waiting / Re-review`; hosted inbox has buckets | **P0 make person-level and durable** |
| Attention reason | Gerrit derivative exposes why/when someone entered attention | Current state explains workflow but not per-participant reason/history | **P0 add** |
| Manual attention override | Users may add/remove people from attention set | No direct equivalent yet | **P1 hosted** |
| Needs-attention dashboard first | First dashboard section is what needs the user | Hosted Inbox now foregrounds Needs review / Returned / Changed | **Near parity** |
| Query-backed dashboard sections | Saved/custom change queries | Hosted inbox has buckets + text search | **P2 saved views / field search** |
| Incoming vs outgoing reviews | Reviewer vs author perspectives are explicit | Hosted buckets partly encode both | **P1 make author/reviewer perspective explicit** |
| Analyzer status chips | Small state chips + detail tab; findings inline but visually distinct | Verification/evidence exists, less Critique-like as a compact global checks strip | **P0 UI opportunity** |
| Analyzer → Please fix | Convert automated finding into unresolved human request | Evidence and annotations exist separately | **P1 add conversion action** |
| Suggested fix | Reviewer can attach/apply structured fix | Existing parity research already marks executable suggestions as a gap | **P0 add** |
| LGTM | Positive reviewer judgment | Finish is intentionally not approval; target judgments are separate | **P1 hosted review verdict** |
| Approval | Separate gatekeeper permission | Host/enterprise policy should own merge permission | **Integrate, do not reinvent** |
| Unresolved count | Visible part of scoring panel | Open feedback and re-review counts now visible | **Parity conceptually** |
| Ready-to-submit banner | Strong green overall state | `Ready to finish` exists, but not host merge readiness | **Keep separate: Review ready vs Host ready** |
| Review/request triggers presubmits | Checks run at useful workflow boundaries | Verification evidence exists; trigger policy is not the Review UI's job | **Integrate** |
| Side-by-side / overlay diff | Both available; minimal chrome around diff | split/unified already present | **Parity direction** |
| Ignore whitespace | Reviewer preference | Not a first-class Review control today | **P1 useful** |
| Move detection | Distinguishes moved from deleted+added | Not a first-class reader concept | **P2 unless diff substrate already provides it** |
| Keyboard navigation | Strong shortcut-driven review | LemonCrow has command palette/shortcuts | **Parity** |
| Artifact diffs | Screenshot/config-generated diffs | Web/API/Markdown/media/evidence surfaces go beyond Critique here | **LC stronger** |
| Review archaeology | Full evolution + comments remains useful after submit | Durable review/revision/activity history exists | **Keep; improve navigation** |
| Post-submit comments | Historical change can still receive context | Historical revisions are intentionally read-only today | **P2; probably hosted-only** |
| Event notifications | Critique emits events; other systems notify users | Review activity/provider events exist; notification product is separate | **Integrate, don't bake notification channels into reader** |

---

## 4. Where the current Critique-inspired LemonCrow branch already matches well

The current `feat/review-ui-polish` / correction-loop work already closes several of the most valuable Critique gaps:

### 4.1 Atomic review handoff

LemonCrow now has a durable correction operation rather than one agent invocation per comment:

```text
human comments
  ↓
unpublished annotation versions
  ↓
immutable feedback operation
  ↓
one exact author-session continuation
  ↓
per-annotation author claims
  ↓
human re-review
```

This is the right adaptation of Critique's `Reply (N drafts)` model for agent-authored changes.

### 4.2 Duplicate-send prevention

Publication is version-aware. An unchanged human comment that was already handed to the author is not silently sent again. Editing the human comment creates new publishable feedback. An author's `addressed` claim does not create new human feedback.

### 4.3 Human-owned resolution

This deliberately differs from Critique. Critique lets an author use `Done`/`Ack` to resolve. For agent-authored work, LemonCrow should keep the safer split:

```text
author says addressed ≠ reviewer says resolved
```

The author claim puts the thread in **Re-review**.

### 4.4 Turn-oriented reader chrome

The Reader now exposes states such as:

- Your turn
- Feedback ready to publish
- Waiting for author
- Re-review requested
- Ready to finish
- Historical revision

This is a useful first layer of the Critique attention-set idea.

### 4.5 Attention-first hosted inbox

The hosted inbox already has buckets that map naturally onto Critique's attention model:

- Needs my review
- Returned to me
- Waiting on author
- Changed since my review
- Ready
- Finished recently

This is close to Critique's dashboard philosophy, where `Needs attention` is the first/default section.

### 4.6 Review progress is finer than Critique

Do **not** copy Critique's file checkbox as LemonCrow's canonical state.

Critique clears the whole file's reviewed flag when the file changes. LemonCrow can preserve valid judgments within a changed file and reopen only targets whose content/evidence changed. That is a meaningful improvement.

The file header may expose a convenient derived `file complete` action/state, but it should remain a projection over targets.

---

## 5. Highest-value Critique parity gaps now

## P0.1 — Make attention a durable **set of actors**, not only a derived banner

The current `Your turn / Waiting for author` strip is useful but is effectively one workflow summary. Critique's deeper idea is an explicit set of **people expected to act**.

Proposed hosted model:

```text
ReviewAttention
  review_id
  actor_id
  actor_type: human | author_agent
  reason:
    review_requested
    reviewer_commented
    feedback_published
    author_responded
    revision_changed
    required_review
    manual
  source_event_id
  added_at
  added_by
  cleared_at?
  cleared_by?
```

Rules should be boring and inspectable, not AI-inferred.

Example transitions:

```text
review requested      -> reviewer enters attention
reviewer publishes    -> reviewer leaves; author/agent enters
agent response/rev N+1 -> author leaves; reviewer enters
reviewer finishes     -> reviewer leaves
manual handoff        -> explicit override
```

Important: bots/checks should not themselves occupy human attention. Their failures may add the owner/reviewer with a reason.

The UI should show **who, why, and since when**. Gerrit's Critique-derived design explicitly exposes those three facts and is a good modern implementation reference.

## P0.2 — Add first-class arbitrary revision comparison

Critique lets users compare any two snapshots. LemonCrow has the harder backend part already: immutable revisions and reconciliation history.

Add:

```text
Compare revisions
  Base: rev 4
  Head: rev 7

  [Changed since my review]
  [All changes]
```

For each target/file show:

- content diff between selected revisions;
- comments present at either revision;
- human mark at each revision;
- preserved/reopened/discarded reason;
- verification/evidence that existed at each revision.

This is more valuable than a generic Git commit compare because it explains **human judgment evolution**.

## P0.3 — Add a compact per-file revision chain

Critique's most interesting file UX idea is the compact snapshot chain. It collapses uninteresting snapshots and highlights snapshots with review/comments/tests.

Adapt it rather than copying drag-and-drop literally:

```text
File history

rev 2 ─ rev 3 ─● rev 5 ─● rev 7
               ^ comment  ^ reviewed
                         current
```

Markers worth showing:

- source changed;
- human judgment recorded;
- comment added/resolved/addressed;
- evidence/check status changed;
- rename;
- current selected comparison bounds.

Default comparison should be **last revision this reviewer judged → current**.

## P0.4 — Turn verification into a Critique-like compact checks strip

Critique puts analyzer state near the top of the change and lets reviewers jump directly to findings. LemonCrow has richer evidence, but it is easier to miss globally.

Add a small strip near the review-state header:

```text
Checks   Tests ✓   Typecheck ✓   Build ✓   2 findings   Coverage ?
```

Requirements:

- distinguish PASS / FAIL / RUNNING / NOT_RUN / UNKNOWN;
- click jumps to evidence/findings;
- failing checks do not automatically become human review comments;
- offer `Request fix` to turn a finding into a human-owned `request_change` annotation;
- keep automated finding styling distinct from human discussion styling.

## P0.5 — Give `Publish feedback` a Critique-like review summary

Critique's Reply dialog is not just a send button. It is the reviewer’s last chance to inspect the atomic handoff.

LemonCrow's feedback panel should eventually show:

```text
Publish feedback

4 unpublished comments
  3 request changes
  1 comment

Targets covered: 3
Files covered: 2
Destination: exact Claude session

[review list]

Optional summary message

[Copy] [Publish feedback]
```

Do not add comment-per-agent orchestration. The batch remains one correction round.

---

## 6. P1 parity worth doing after the P0 state model

### P1.1 Private drafts for hosted multi-user review

Critique's comments are true reviewer-local drafts until atomic publish. LemonCrow's local mode does not need secrecy, but hosted peer review does.

A hosted annotation should eventually distinguish:

```text
draft visibility: author_only | published
```

or equivalent reviewer-owned draft storage.

This allows a reviewer to revise their complete thought before other humans react piecemeal.

### P1.2 Human review verdict separate from merge approval

Critique separates `LGTM` from `Approval`. LemonCrow should preserve that conceptual split without rebuilding merge policy.

Possible hosted state:

```text
review verdict: pending | looks_good
host approval: imported provider state
open human concerns: N
```

Negative review state should continue to be represented by concrete request-change comments, not a vague `thumbs down` score.

### P1.3 Author/reviewer perspectives in inbox

Critique clearly separates incoming and outgoing review work.

Hosted LemonCrow should make these two perspectives obvious:

- **Needs you** — your attention set.
- **Authored by you** — waiting on reviewers / author action / ready.

Current buckets contain most of the necessary data; this is primarily organization and state projection.

### P1.4 Ignore-whitespace and diff preferences

Critique treats diff quality as core UX. LemonCrow already has split/unified and themes. Add ignore-whitespace controls if the diff substrate supports them safely without changing target identity unexpectedly.

Any display-only normalization must not alter ReviewTarget identity or review-state reconciliation unless the underlying source identity actually changes.

### P1.5 `Request fix` from automated evidence

Critique can convert analyzer findings into human unresolved comments with `Please fix`.

LemonCrow should allow:

```text
automated evidence/finding
  ↓ human clicks Request fix
human request_change annotation
  ↓
normal correction round
```

This preserves authority boundaries cleanly.

---

## 7. Critique ideas LemonCrow should deliberately **not** copy literally

### 7.1 File-level reviewed state as canonical truth

LemonCrow's content-bound target reconciliation is better. Keep file completion as a derived convenience only.

### 7.2 Author-controlled final resolution for agent review

Critique's trust model lets the author resolve via Done/Ack. That works in human teams. For agent-authored work, keep the current separation: author claims addressed; human owns resolution.

For fully human peer review, hosted policy may later allow Critique-like author resolution as an option, but it should not weaken the agent-review default.

### 7.3 Merge/submit policy engine

LGTM/Approval/submit requirements are valuable concepts, but repository hosts already own merge authorization. LemonCrow should import and explain host approval state rather than becoming another branch-protection system.

### 7.4 A universal development portal

Critique explicitly rejected becoming `Code Central`. LemonCrow should make code search, agent sessions, tests, traces, deployments and editors one click away, but Review should remain focused on human judgment.

### 7.5 Over-customizable review state

Critique is intentionally opinionated. Custom dashboard views are useful; custom semantics for every team are not.

Keep core state vocabulary small and durable.

---

## 8. Recommended implementation order

### Phase A — state parity

1. Person-level durable Attention Set + transition log.
2. Wire attention transitions to request review, publish feedback, author response/new revision, finish.
3. Show `who / why / since when` in Reader and Hosted Inbox.
4. Make inbox `Needs you` a true attention query, not only derived bucket logic.

### Phase B — revision parity

5. Arbitrary ReviewRevision A/B selection.
6. Default `last judged → current` compare.
7. Compact per-file revision chain with comment/review/check markers.
8. Target judgment history inside compare.

### Phase C — checks + publish polish

9. Compact checks/evidence strip under the review state header.
10. Automated finding → `Request fix` conversion.
11. Rich Publish Feedback summary grouped by kind/target/file.
12. Optional round-level reviewer summary message.

### Phase D — hosted peer-review parity

13. Reviewer-private draft comments.
14. Explicit human `looks good` verdict distinct from provider approval.
15. Incoming vs authored review perspectives / saved inbox views.
16. Notification events sourced from the durable attention set.

---

## 9. Product rule to keep

Critique's most transferable lesson is not a widget. It is this invariant:

> **At every moment, a reviewer should be able to answer: what changed, what have I already reviewed, what still needs judgment, whose turn is it, and what exact action moves the review forward?**

LemonCrow can answer this more precisely than Critique because it has content-bound ReviewTargets, exact author-agent provenance, durable correction rounds, and behavior/evidence surfaces. The parity work should make that state **as obvious as Critique made file/snapshot/attention state**, without flattening LemonCrow back down to files and pull-request votes.
