# LemonCrow Review Reader — Canonical Product, UX, and Engineering Spec

> **Status:** canonical replacement direction
>
> **Date:** 2026-09-12
>
> **Primary user:** senior software engineer reviewing human- and agent-produced code
>
> **Primary surface:** local browser workspace opened by `lc review --open`
>
> **North-star workflow:** understand → inspect → judge → comment → iterate → re-review only what changed
>
> **Supersedes for product/UX direction:** the browser-workspace sections of `docs/planning/2026-09-08-review-first-developer-workspace.md` and the deleted `2026-09-10-world-class-reviewer-plan.md`
>
> **Does not supersede:** the existing durable review session, revision, annotation, anchoring, provenance, evidence, and reconciliation contracts unless this document explicitly changes one.

---

## 0. Executive decision

LemonCrow Review is no longer designed as a three-pane review dashboard.

It is designed as a **manual review reader**.

The code diff is the product. LemonCrow intelligence exists to reduce how much code a human must read, preserve prior human judgment across agent revisions, and answer questions that arise while the human is reading. It must not compete with the diff for attention.

The product should feel like this:

```text
open change
   ↓
LC orders the work
   ↓
read code continuously
   ↓
mark one meaningful review target at a time
   ↓
open context only when needed
   ↓
leave precise comments in place
   ↓
send feedback to author/agent
   ↓
author changes code
   ↓
LC preserves valid judgments
   ↓
review only changed/new targets
   ↓
finish with explicit remaining-risk summary
```

The core differentiator is not a nicer diff renderer and not an AI summary.

It is:

> **After the author changes the code, LemonCrow tells me exactly which of my prior judgments are still valid and puts only the invalidated/new work back in front of me.**

Everything in this spec serves that loop.

---

## 1. Product thesis

Agentic coding changes the bottleneck from code production to human understanding.

A senior engineer does not need another system that writes a long review on their behalf. They need a system that lets them spend human judgment efficiently and safely.

LemonCrow therefore optimizes five quantities:

1. **Time to orientation** — how quickly the reviewer understands the shape of the change.
2. **Time to first meaningful judgment** — how quickly they are reading real code rather than product chrome.
3. **Judgment precision** — whether “reviewed” attaches to the actual thing the human inspected.
4. **Judgment durability** — whether valid prior review survives an author revision without asking the human to re-read unchanged work.
5. **Context-switch cost** — how rarely the reviewer must leave the review surface for IDE, terminal, GitHub, or chat just to understand the change.

The browser workspace succeeds only if a senior engineer can use it as the primary manual review surface rather than as an intelligence companion beside another diff viewer.

---

## 2. Product principles

### 2.1 The diff owns the screen

At least 70% of useful desktop width should be available to code in the normal review state.

Permanent side panels are a cost. Any panel must prove that it deserves to be visible while code is being read.

Default state:

- compact review outline visible;
- continuous diff stream visible;
- context drawer closed;
- no large review brief above the code;
- no dashboard cards in the primary reading path.

### 2.2 Human judgment is the authority

LemonCrow may:

- order review work;
- expose impact;
- expose changed symbols;
- expose verification;
- expose provenance;
- preserve/reopen prior marks;
- explain why a target deserves attention.

LemonCrow must not silently transform those signals into:

```text
AI APPROVED
SAFE TO MERGE
NO ISSUES
```

A human review mark means only that a human recorded a judgment over a specific content identity.

### 2.3 Review meaningful units, not filenames

A file is navigation and containment. It is not automatically the unit of human judgment.

One file can contain:

- four independent hunks;
- two changed public methods;
- generated changes;
- a config edit;
- a deletion;
- an unrelated formatting change.

The reviewer must be able to mark the meaningful portions independently.

### 2.4 Do not double-count overlapping units

LemonCrow already derives file, hunk, and symbol units. Those are useful identities, but they overlap.

The UI must never tell the reviewer that 39 files + 129 hunks + 162 symbols equals 330 independent pieces of work.

A new derived layer, `ReviewTarget`, selects the **non-overlapping units that constitute review progress for the current revision**.

### 2.5 Intelligence appears at the point of need

A reviewer reading a changed call site needs a small indication that the symbol has 12 out-of-patch callers.

They do not need a permanent 420 px evidence desk explaining all possible facts before they have asked a question.

Default intelligence is compact and local:

```text
⚠ public contract · 12 external callers · 1 check unknown
```

Detailed evidence opens on demand.

### 2.6 “Changed since I reviewed” is sacred

A reviewed judgment may survive only when LemonCrow can justify that the reviewed content is the same.

A changed target reopens visibly.

An unchanged target does not reopen merely because another file changed.

A relocated annotation is never silently presented as exact.

### 2.7 Unknown remains first-class

Unknown is not zero, safe, absent, or passed.

Examples:

- unknown caller count is not zero callers;
- tests not observed are not passing tests;
- an unrecorded file read is not proof the author skipped it;
- an un-fingerprintable unit is not a durable reviewed target;
- ambiguous annotation relocation is not a relocated annotation.

### 2.8 Reviewer flow beats feature visibility

Features that are available but hidden behind a key, drawer, menu, or command palette are acceptable.

Features that permanently reduce code readability merely so they remain visible are not.

---

## 3. Non-goals

This release is not trying to become:

- a full GitHub replacement;
- a code editor;
- a repository explorer;
- an autonomous merge authority;
- an AI review essay generator;
- a project-management tool;
- a general-purpose IDE;
- a live multi-user collaboration product;
- a hosted PR inbox.

Those may become integrations or future surfaces. They must not distort the local manual-review workflow.

---

## 4. Existing capabilities to preserve

The redesign must reuse and protect the strong parts already implemented.

### 4.1 Durable `ReviewSession`

The session remains the durable object. It is not tied to one browser tab or one agent invocation.

### 4.2 Immutable `ReviewRevision`

The reviewer reads a frozen revision. A changing working tree creates an available-new-revision signal; it does not mutate the code currently under the reviewer.

### 4.3 File/hunk/symbol `ReviewUnit`s

Existing unit identity and fingerprints remain the raw substrate.

### 4.4 Reconciliation and review frontier

Existing mark reconciliation remains the basis for:

- carried judgments;
- reopened judgments;
- newly introduced units;
- removed units;
- discarded verdicts.

### 4.5 Robust annotation anchoring

Keep the relocation ladder, exact-vs-heuristic distinction, orphan state, and server-owned anchors.

### 4.6 Evidence and visual artifacts

Keep screenshots, images, videos, Playwright traces, documents, and live previews bound to a revision.

### 4.7 Provenance

Keep exact provenance when available and preserve uncertainty when attribution is not exact.

### 4.8 Feedback export/delivery

Keep structured feedback export and direct delivery where exact author-session provenance permits it.

---

## 5. Core vocabulary

### 5.1 `ReviewUnit`

A persisted identity/fingerprint unit derived from the change.

Current kinds:

```text
file
symbol
hunk
document_section   # future/non-code surface
```

These units may overlap.

### 5.2 `ReviewTarget`

A **derived, non-overlapping judgment target** shown in the manual review reader and counted toward progress.

A target references exactly one underlying `ReviewUnit`.

A target is not persisted as a second source of truth. It is deterministically derived from the current revision's units and patch geometry.

### 5.3 `ReviewOutlineItem`

A navigation row. Normally one changed file, but it can summarize target-level state beneath it.

### 5.4 `ReviewMark`

The reviewer's durable judgment attached to the underlying target unit.

### 5.5 `ReviewFrontier`

The current set of target judgments that remain valid, reopened, unresolved, new, or unreviewed.

### 5.6 `AttentionSignal`

A deterministic reason to raise a target's review priority.

Examples:

- public contract changed;
- high-centrality symbol;
- external callers;
- security-sensitive path;
- migration/configuration change;
- failed verification;
- unknown verification;
- author did not inspect an impacted file where exact read provenance exists;
- changed since previous human review.

### 5.7 `ContextDrawer`

The on-demand secondary surface containing evidence and impact details for the currently focused target/file.

It is closed by default.

---

## 6. ReviewTarget derivation

This is the most important new backend projection.

### 6.1 Why it exists

The raw unit graph is intentionally redundant:

```text
file
 ├─ hunk 0
 │   ├─ symbol A
 │   └─ symbol B
 ├─ hunk 1
 │   └─ symbol B
 └─ hunk 2
```

Counting all of those as review work produces meaningless progress and encourages broad file-level approval.

`ReviewTarget` converts the overlapping graph into one set of human-sized judgment targets.

### 6.2 Target selection policy

For each changed file:

1. Start from changed lines represented by renderable hunks.
2. Prefer a trustworthy changed-symbol unit when changed lines are owned by a definition.
3. Use hunk units for changed lines that are not safely attributable to a symbol.
4. Use a file unit only when target-level textual identity is unavailable or when the change itself is file-level metadata.
5. Never produce two targets that claim the same changed lines as independent progress.

### 6.3 Symbol ownership

A symbol may become a target when all are true:

- the symbol has a stable qualified identity;
- the changed new-side lines intersect its extracted definition window;
- the definition window is not flagged ambiguous;
- its fingerprint method is suitable for reconciliation;
- the symbol is not only an incidental enclosing definition when a more specific nested definition owns the changed lines.

For nested definitions, changed lines are assigned to the **deepest trustworthy owner**.

Example:

```python
class Reader:
    def run(self):       # outer symbol
        def normalize(): # inner symbol
            ... changed ...
```

The changed lines belong to `Reader.run.normalize`, not both `Reader.run` and `Reader.run.normalize`.

### 6.4 One symbol can cover multiple hunks

If two separate hunks change the same stable symbol, they form **one symbol ReviewTarget**.

The reader renders the hunks in their natural file positions, but both carry the same target identity and judgment state.

Marking that symbol reviewed settles all changed segments attributed to that target.

### 6.5 Hunk targets

Use a hunk target when:

- the change is outside any trustworthy symbol;
- the language has no symbol extractor;
- the change is configuration/data/text;
- the changed content is deletion-only and cannot map safely to a surviving symbol;
- symbol extraction is degraded/ambiguous for that span;
- the rendered patch body is unavailable **but exact changed-line ranges were preserved independently by the diff producer**.

A deletion-only run must not inherit ownership from the surviving symbol at its new-side join point. The join point proves where the deletion sits, not which definition owned the removed bytes.

Hunk identity remains weaker across re-hunking. The UI must not hide this. If reconciliation resets hunk marks because hunk geometry changed, the revision delta must say so.

### 6.6 File targets

Use the file unit as the actual target only for cases such as:

- binary change;
- submodule pointer;
- mode-only change;
- rename-only/move review where path identity itself matters;
- patch body unavailable **and no exact changed-line ranges survive**;
- any hunk in the file lacks trustworthy target-level geometry, because a partial denominator is worse than a conservative file-level judgment;
- no trustworthy hunk identity can be produced.

Ordinary source files must not become one file-level target merely because the browser navigates by file.

### 6.7 Generated/mechanical targets

Generated/vendored targets are still targets, but default to a collapsed `Mechanical` section.

They do not become implicitly reviewed.

The reviewer may explicitly bulk-mark the mechanical section only after the server rechecks eligibility against the current revision.

### 6.8 Target shape

```ts
interface ReviewTarget {
  target_id: string;            // deterministic presentation id
  unit_key: string;             // underlying ReviewUnit
  kind: "symbol" | "hunk" | "file";
  path: string;
  label: string;
  symbol: string;

  start_line: number;
  end_line: number;
  hunk_ordinals: number[];

  state: MarkState;
  changed_since_mark: boolean;
  reviewed_revision_id: string;

  attention_rank: number;
  attention_level: "high" | "normal" | "mechanical";
  reasons: string[];

  additions: number;
  deletions: number;
  fingerprint_method: string;

  verification: {
    pass: number;
    fail: number;
    unknown: number;
  };

  annotation_counts: {
    open: number;
    orphaned: number;
    addressed_needs_rereview: number;
  };
}
```

`target_id` is not a persisted identity. `unit_key` remains the durable mark identity.

### 6.9 Target progress denominator

Progress uses only current `ReviewTarget`s.

Never use:

```text
number of files
all raw ReviewUnits
number of comments
number of lines
```

as the primary review denominator.

Example:

```text
39 changed files
330 raw units
74 derived review targets
52 reviewed
7 changed since review
2 needs changes
13 unreviewed
```

Primary progress:

```text
52 / 74 targets reviewed
```

---

## 7. Judgment state model

Use the existing durable mark states:

```text
unreviewed
reviewed
needs_changes
changed_since_review
unknown
```

### 7.1 Meaning of each state

#### `unreviewed`

No durable human judgment exists for the target's current content.

#### `reviewed`

The human explicitly judged the target content represented by the stored fingerprint.

This is not approval of the whole change and not proof of correctness.

#### `needs_changes`

The human has recorded that this target requires author action.

This state survives author revisions until the reviewer explicitly changes it, but the UI must additionally indicate when the author claims the associated comments were addressed.

#### `changed_since_review`

The human previously reviewed this target identity, but the current content does not match the content they reviewed.

This state is first-class and visually stronger than ordinary unreviewed work.

#### `unknown`

LemonCrow cannot make the normal content-identity claim required for durable review state.

Unknown never renders as reviewed.

### 7.2 No durable “visited” state

Simply scrolling a target into view is not a judgment.

The browser may track ephemeral per-tab `visited` state to help navigation, but it must never survive as a review mark or count toward progress.

### 7.3 Marking a target

Primary action:

```text
r = mark current target reviewed + advance
```

Secondary actions:

```text
x = needs changes + keep focus
u = reopen/unreview
```

The browser posts the target's `unit_key`, not a file path.

### 7.4 File-level completion

A file row may display:

```text
3/4
```

meaning three of four derived targets are settled as reviewed.

A file is visually “done” when every non-mechanical target is reviewed and there are no unresolved human requests requiring re-review.

This file-level state is derived. It is not a broad file mark silently replacing target marks.

### 7.5 Explicit bulk completion

A reviewer may choose:

```text
Mark remaining targets in this file reviewed
```

or:

```text
Mark remaining ordinary targets in this section reviewed
```

The server must re-evaluate every proposed target against the current revision and refuse:

- high-attention targets;
- changed-since-review targets;
- needs-changes targets;
- unknown-fingerprint targets;
- targets with open request-change comments;
- targets whose current file-scoped verification is failed or unresolved;
- stale proposed target ids.

Target verification is conservative and attributable: only verification evidence that is current for the reviewed code and explicitly scoped to the target's file may populate a target's pass/fail/unknown counts. Review-wide packet checks or global evidence remain review context and must never be projected onto every target. Repeated file checks are one logical check per title; the newest current result wins.

Bulk review is convenience, never an escape hatch around attention.

---

## 8. Review ordering

The default reader has exactly one recommended order.

Do not expose four equal-weight conceptual lenses as the primary navigation model.

### 8.1 Default ordering algorithm

Order targets approximately by:

1. changed since my review;
2. failed verification on affected target/file;
3. public/API/schema/contract changes;
4. high-centrality definitions;
5. targets with out-of-patch impact;
6. security/auth/data-boundary changes;
7. foundational dependencies before dependents;
8. ordinary implementation;
9. tests;
10. documentation;
11. generated/mechanical.

Within a file, preserve source order unless doing so would split one multi-hunk symbol target nonsensically.

### 8.2 Review order is explainable

Every promoted target has one or more concrete reasons.

Never show an opaque numeric “risk score” as the justification.

Allowed:

```text
#3 · public contract changed · 12 callers outside patch
```

Not allowed:

```text
Risk 87
```

### 8.3 Alternate ordering

Alternate views live under one small `Order` control or command palette.

Available when the target projection can prove the ordering:

```text
Recommended
File order
```

Reserved until the backend carries deterministic target-level provenance for them:

```text
Commit order
Dependency order
```

Do not expose a mode whose ordering substrate is missing; a plausible-looking client-side sort is worse than saying the mode is unavailable.

`Intent` becomes orientation metadata, not a permanent top-level review mode.

Switching order changes navigation only. It never changes marks or target identity, and a revision refresh preserves the reviewer-selected order.

---

## 9. Workspace information architecture

### 9.1 Default desktop layout

```text
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ Review · rev 7   74 targets   52 reviewed   7 changed   2 needs changes      Finish │
│ Usage model · review capture · gateway                                        ⋯      │
├──────────────────┬───────────────────────────────────────────────────────────────────┤
│ REVIEW OUTLINE   │                                                                   │
│                  │  src/.../gitdiff.py                                  +151 −13      │
│ ⚠ gitdiff.py 2/5 │  public contract · 12 external callers · 1 unknown check          │
│ ↻ run_ledger 1/3 │  ─────────────────────────────────────────────────────────────     │
│ ○ local.py   0/2 │  @@ ...                                                           │
│ ✓ spend.py   3/3 │       actual code                                                 │
│                  │       actual code                            ⚠ 12 callers          │
│ Tests        4/6 │                                                                   │
│ Mechanical   0/8 │       actual code                                                 │
│                  │  [r Reviewed + next] [c Comment] [e Context]                      │
│                  │                                                                   │
│                  │  ─────────────────────────────────────────────────────────────     │
│                  │  next target ...                                                  │
├──────────────────┴───────────────────────────────────────────────────────────────────┤
│ j/k target  J/K file  r reviewed  x changes  c comment  e context  / find   52/74   │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### 9.2 Widths

Default desktop:

- outline: `232–272 px`, default `248 px`;
- center reader: all remaining width;
- context drawer: hidden;
- context drawer open: `360–480 px`, overlay preferred below `1440 px`, docked only on very wide screens or explicit pin.

The existing permanent `20rem + 420px` allocation is removed.

### 9.3 Vertical hierarchy

The code begins immediately below a compact orientation bar.

The following must not permanently sit above the diff:

- Review Brief card;
- Major changes row;
- Review first row;
- large verification strip;
- large progress dashboard;
- multiple persistent alert banners.

Important state becomes a compact single-line notification or a dismissible/expandable sheet.

---

## 10. Top orientation bar

The top bar answers four questions only:

```text
What am I reviewing?
Which revision?
How much judgment remains?
Can I finish?
```

### 10.1 Left side

```text
Review · rev 7
Usage model · review capture · gateway
```

The second line is the deterministic change story. It truncates gracefully.

### 10.2 Center/compact status

```text
74 targets · 52 reviewed · 7 changed · 2 needs changes
```

Optional small verification indicator:

```text
checks 18✓ 1! 2?
```

### 10.3 Right side

Primary:

```text
Finish review
```

Secondary `⋯` menu:

```text
Refresh revision
Prepare feedback
Send feedback to author/agent   # when eligible
Review history
Order…
Split/unified
Toggle context
Discard review
```

Do not put six peer buttons in the primary header.

---

## 11. Review outline

The left outline is navigation, not a dashboard.

### 11.1 Row shape

```text
⚠ gitdiff.py                 2/5
  public contract · 12 callers
```

or compactly:

```text
↻ run_ledger.py             1/3
```

### 11.2 Status glyphs

Suggested semantics:

```text
⚠  contains high-attention target
↻  contains changed-since-review target
!  contains needs-changes target
○  ordinary unreviewed remains
✓  all reviewable targets settled
·  mechanical/generated only
?  contains unknown target
```

Glyph plus text/tooltip; never color alone.

### 11.3 Sections

Default sections:

```text
Needs attention
Changed since review
Remaining
Tests
Mechanical
Done          # collapsed by default
```

These are file summaries derived from target state.

Do not duplicate the same file in multiple visible sections. A file belongs to the highest-priority section that describes its outstanding targets.

### 11.4 Search

`/` focuses search.

Search matches:

- path;
- symbol;
- attention reason;
- annotation text/title where cheap;
- check name where applicable.

Search narrows navigation but does not mutate the underlying review order.

### 11.5 Collapse

The entire outline can collapse to a slim rail with one key/button.

Focus mode should allow effectively full-width code.

---

## 12. Continuous review stream

### 12.1 Core decision

The center surface becomes one scrollable review stream rather than one selected file replacing another.

Files appear in the selected review order.

### 12.2 File boundary

Each file begins with a sticky lightweight header:

```text
src/lemoncrow/pro/capabilities/review/gitdiff.py      modified     +151 −13      2/5
```

Actions on hover/focus:

```text
Open file context
Collapse file
Mark eligible remainder reviewed
```

### 12.3 Target boundary

Every target has a subtle target header, not a giant card.

Symbol example:

```text
Reader.run · modified        ⚠ public contract · 12 callers        ↻ changed since review
```

Hunk example:

```text
Lines 184–227 · config change                                  ○ unreviewed
```

### 12.4 Target actions

At the end of or sticky within the target:

```text
Reviewed + next   Request changes   Comment   Context
```

Keyboard remains the faster path.

### 12.5 Virtualization

Large reviews must remain a single conceptual stream while rendering only a bounded window.

Requirements:

- preserve scroll anchor when targets above are measured;
- keyboard next/previous target may jump into an unmounted region;
- comments and annotations must mount at their correct target/line;
- sticky file headers must work under virtualization;
- target completion must not cause unexpected scroll jumps;
- refresh must preserve the closest still-existing target identity.

### 12.6 Progressive loading

The initial workspace must not fetch all full patch bodies before becoming useful.

Initial load:

- overview;
- derived target metadata;
- outline;
- first viewport's file patches.

Then prefetch:

- next one or two files in review order;
- context metadata for focused target;
- annotation markers needed for loaded files.

---

## 13. Inline intelligence

Inline intelligence is a hint, not prose.

### 13.1 What may appear inline

Examples:

```text
⚠ public contract
12 external callers
2 affected files outside patch
1 failed check
2 unknown checks
author did not inspect affected caller   # only with exact read provenance
comment addressed · re-review
```

### 13.2 What must stay out of the code path

Do not place these as permanent paragraphs between hunks:

- full provenance narrative;
- full command history;
- all affected-call-site snippets;
- full verification evidence;
- screenshots/video gallery;
- multi-paragraph AI explanation.

Those belong in Context.

### 13.3 Click behavior

Clicking an inline signal opens Context directly to the relevant section.

Example:

```text
12 external callers
```

opens:

```text
Context → Impact
```

not generic Summary.

---

## 14. Context drawer

### 14.1 Default state

Closed.

Open with:

```text
e
```

or click an inline signal.

Close with:

```text
Esc
```

### 14.2 Drawer tabs

Use task-oriented tabs:

```text
Impact
Checks
Evidence
Author
Discussion
```

Do not start with a generic Summary tab that restates information already visible around the diff.

### 14.3 Impact

Shows:

- changed definitions;
- caller count;
- external impacted sites;
- in-patch impacted sites;
- related source snippets from the reviewed revision;
- uncertainty/degraded signals.

Clicking an in-patch site scrolls the continuous stream to that target/file.

Clicking an out-of-patch site opens a temporary source peek in the drawer or a center overlay without losing the review scroll position.

### 14.4 Checks

Shows relevant checks in this order:

```text
failed
unknown / not run
passed
```

File-scoped checks before review-wide checks.

Never turn review-wide pass counts into proof about one target.

### 14.5 Evidence

Shows current-revision artifacts first.

Stale artifacts live behind `Previous revisions`.

Every stale artifact explicitly says it belongs to older code.

### 14.6 Author

Shows:

- exact authoring host/model/session when known;
- author intent;
- relevant commands run;
- whether the file was inspected only if read capture is actually supported;
- provenance uncertainty otherwise.

### 14.7 Discussion

Shows threads affecting the focused target/file, including:

- human comments;
- author responses;
- LemonCrow findings;
- optional AI-review findings.

Human judgment remains visually distinct from tool/agent claims.

### 14.8 Pinning

Very wide desktops may pin Context.

Pinning is an explicit user choice remembered in session storage. It is not the default product state.

---

## 15. Commenting and review requests

### 15.1 Comment kinds

For new human comments, the primary UI supports:

```text
Comment
Request changes
Suggestion
```

`looks_good` is removed from the new composer UI.

Reason: “looks good” duplicates durable review state and creates two competing ways to express the same human judgment.

The backend may continue accepting/rendering legacy `looks_good` annotations for compatibility.

### 15.2 Creating comments

Supported gestures:

- gutter `+` on a line;
- drag-select line range;
- `c` for current-target/file-level comment;
- command palette `Comment current target`.

### 15.3 Comment composer

Composer remains inline at the anchored location.

Keyboard:

```text
Cmd/Ctrl+Enter = submit
Esc            = cancel
```

Submission must not move the reviewer unless the user explicitly chooses a “submit + next” flow later.

### 15.4 Request changes and target state

Creating a human `request_change` comment should offer to mark the current target `needs_changes` in the same transaction/interaction.

Default: yes.

The reviewer can make a non-blocking comment without changing target state.

### 15.5 Author says addressed

When feedback returns and the author claims a request is addressed:

```text
author says addressed · re-review
```

The human's request does not resolve itself.

The target returns to the high-priority queue when relevant content changed.

### 15.6 Anchor confidence

Exact unchanged anchors should be quiet.

Heuristic relocations should carry a small visible indicator.

Orphaned comments must appear in a dedicated unresolved area and never be drawn on a guessed line.

---

## 16. Navigation and keyboard model

Keyboard review is first-class.

### 16.1 Required shortcuts

```text
j / k        next / previous ReviewTarget
J / K        next / previous file
] / [        next / previous high-attention or changed target
r            mark current target reviewed and advance
u            reopen current target
x            mark current target needs changes
c            comment current target/file
s            split / unified diff
/            search
f            toggle outline/focus mode
e            toggle Context drawer
p            command palette / actions
?            shortcuts
Esc          close transient UI / cancel compose
```

### 16.2 Navigation semantics

`j/k` always traverse **targets**, never arbitrary files.

The selected/current target is whichever target owns the focused diff region.

Scrolling manually updates the current target once the next target header crosses a stable focus threshold.

### 16.3 `r` behavior

`r`:

1. marks the current target reviewed;
2. waits for server confirmation;
3. advances to the next actionable target in the current filtered/review order;
4. never jumps to a hidden target outside the user's active search/filter.

If the mark is downgraded/refused, do not advance silently.

### 16.4 Mouse-only parity

Every keyboard action must have an accessible clickable equivalent.

Keyboard shortcuts are acceleration, not the only interaction method.

---

## 17. Orientation before reading

The reviewer needs a compact change orientation, not a report.

### 17.1 Change story

Render one line beneath the title:

```text
Usage accounting · review capture · CLI integration · frontend reader
```

It is deterministic and derived from existing intent/chapter logic.

### 17.2 Expandable overview

Clicking the change story opens a small sheet with:

- major conceptual changes;
- files/targets per change;
- verification summary;
- provenance summary;
- current/stale evidence counts.

This replaces the permanently expanded `Review brief`, `Major changes`, and `Review first` rows.

### 17.3 Review-first hints

The outline and default ordering already express “review first.”

Do not repeat a second horizontal row of review-first buttons above the diff.

---

## 18. Revision update flow

This is LemonCrow's flagship workflow.

### 18.1 Detecting change

For working-tree/staged reviews, keep automatic source probing.

Do not mutate the current revision when source changes.

Compact banner/toast:

```text
New revision available · 6 paths changed     Review update   Later
```

### 18.2 Accepting the revision

When the reviewer chooses `Review update`:

1. snapshot/build the new revision;
2. derive raw units;
3. reconcile marks;
4. relocate annotations;
5. derive new `ReviewTarget`s;
6. map old visible target to best surviving new target;
7. update stream;
8. show revision delta summary.

### 18.3 Revision delta summary

Required summary:

```text
Revision 8
7 targets need your eyes
58 judgments preserved
4 new targets
3 changed since review
1 prior target removed
0 comments orphaned
```

The first line must answer the human question:

> What do I need to look at now?

### 18.4 Queue after revision

Default navigation jumps to:

1. changed-since-review targets;
2. author-addressed requests requiring re-review;
3. new high-attention targets;
4. new ordinary targets.

Preserved reviewed targets remain collapsed/de-emphasized.

### 18.5 Discarded verdicts

If reconciliation removes a unit carrying a human verdict, state it explicitly.

Example:

```text
1 prior reviewed target left the change: old_parser.py::parse
```

This stays available in revision history and is not a one-request transient message.

### 18.6 Hunk re-hunking

If hunk identities reset because hunk count/geometry changed:

```text
2 hunk judgments reopened because this file was re-hunked; symbol judgments were preserved where identity remained stable.
```

Never make this look like arbitrary loss of state.

---

## 19. Finishing a review

### 19.1 Finish is a human decision

LemonCrow must not prevent the human from finishing merely because unresolved items exist.

It must make those items impossible to overlook.

### 19.2 Finish sheet

Clicking `Finish review` opens a compact sheet:

```text
Finish review

52 / 74 targets reviewed
7 changed since your review
2 need changes
4 unreviewed
1 unknown
2 open comments
1 orphaned comment
1 failed check
2 checks unknown

[Return to changed targets]
[Return to requests]
[Finish with outstanding work]
```

When clean:

```text
74 / 74 targets reviewed
0 open requests
verification: 18 pass · 0 fail · 0 unknown

[Finish review]
```

### 19.3 No false completion percentage

The main progress percentage/count must derive from `ReviewTarget` state.

Do not calculate completion from file rows while target-level review is active.

### 19.4 Finished review remains readable

A finished review is read-only in judgment semantics except explicit `Reopen review`.

A new source revision may be detected, but accepting it should reopen the review explicitly.

---

## 20. Feedback flow

### 20.1 Prepare feedback is secondary

Move `Prepare feedback` out of the primary header into:

- Finish sheet when open comments exist;
- `⋯` menu;
- command palette.

### 20.2 Feedback payload

Feedback bundles contain:

- open request-change comments first;
- suggestions;
- comments;
- orphaned comments in a separate section;
- target/path/symbol context;
- reviewed revision id;
- anchor/evidence metadata where useful.

### 20.3 Direct author delivery

When exact provenance permits direct delivery:

```text
Send 3 requests to Claude session <id>
```

must remain a separate explicit action after previewing the payload.

### 20.4 Returning author response

Author responses are projected back onto the same annotations and targets.

Do not create a parallel “agent feedback” state model.

---

## 21. API contract — V2 reader projection

Do not force the frontend to reconstruct target semantics from overlapping units.

The server owns the projection.

### 21.1 Overview

Extend/replace the browser overview response with:

```ts
interface ReviewReaderOverview {
  session: ReviewSessionInfo;
  revision: RevisionInfo;

  title: string;
  change_story: string[];
  change_story_more: number;

  progress: {
    target_count: number;
    reviewed: number;
    changed_since_review: number;
    needs_changes: number;
    unreviewed: number;
    unknown: number;
    mechanical: number;
  };

  verification: {
    pass: number;
    fail: number;
    not_run: number;
    unknown: number;
  };

  outline: ReviewOutlineItem[];
  sections: ReviewOutlineSection[];

  revision_delta?: RevisionDelta;
  discarded: DiscardedVerdict[];
  degraded: DegradedNote[];
}
```

### 21.2 Targets endpoint

Add:

```text
GET /api/reviews/{review_id}/targets
```

Response:

```ts
{
  revision_id: string;
  order: "recommended" | "file" | "commit" | "dependency";
  targets: ReviewTarget[];
}
```

Optional query:

```text
?order=recommended
```

The endpoint returns metadata, not every patch body.

### 21.3 Stream/file patch endpoint

Keep the existing file patch endpoint initially:

```text
GET /api/reviews/{review_id}/files/{path}/patch
```

Enhance its response with enough mapping for the reader to associate rendered lines/hunks with `ReviewTarget`s:

```ts
interface TargetSpan {
  target_id: string;
  unit_key: string;
  side: "old" | "new" | "both";
  start_line: number;
  end_line: number;
  hunk_ordinal: number;
}
```

The frontend must not infer target ownership from symbol names or line overlap by itself.

### 21.4 Mark endpoint

Existing endpoint already accepts any resolvable unit key:

```text
POST /api/reviews/{review_id}/marks
```

The new reader posts target unit keys.

Return updated target state/progress, not a full rebuild of file-only attention groups.

V2 response:

```ts
{
  unit_key: string;
  requested: MarkState;
  recorded: MarkState;
  downgraded: boolean;
  note: string;
  target: ReviewTarget;
  progress: ReviewProgress;
  outline_updates: ReviewOutlineItem[];
}
```

This avoids refetching/re-rendering the entire review on every `r` keystroke.

### 21.5 Bulk endpoint

Generalize bulk marking from file-only units to eligible target keys:

```text
POST /api/reviews/{review_id}/marks/bulk
```

Server performs current-revision eligibility checks.

### 21.6 Context endpoint

The existing file patch/evidence/related-source APIs can remain, but add a compact target-context projection if assembling context becomes chatty:

```text
GET /api/reviews/{review_id}/targets/{target_id}/context
```

Potential response:

```ts
{
  target: ReviewTarget;
  impact: ImpactSite[];
  symbols: SymbolInfo[];
  verification: EvidenceRow[];
  provenance: ProvenanceInfo;
  annotations: Annotation[];
  evidence: ReviewEvidence[];
}
```

Implement only if measurement shows the current routes produce unnecessary round trips.

### 21.7 Revision delta

`POST /refresh` should return target-level reconciliation summary in addition to raw unit delta:

```ts
interface RevisionDelta {
  revision_number: number;
  needs_attention: number;
  preserved_reviewed: number;
  changed_since_review: number;
  new_targets: number;
  removed_targets: number;
  orphaned_comments: number;
  discarded_verdicts: number;
  notes: string[];
}
```

---

## 22. Backend module plan

### 22.1 New module: `targets.py`

Add:

```text
src/lemoncrow/pro/capabilities/review/targets.py
```

Responsibilities:

- derive non-overlapping `ReviewTarget`s;
- assign changed spans to deepest trustworthy symbol;
- fall back to hunk/file targets;
- aggregate target state from marks/frontier;
- derive target progress;
- derive file/outline summaries;
- derive target ordering keys;
- map raw reconciliation to target-level revision delta.

Pure module where practical. No FastAPI/UI logic.

### 22.2 Keep `units.py` raw

Do not mutate `derive_units` into a presentation algorithm.

`ReviewUnit` remains the identity/fingerprint substrate.

### 22.3 `revisions.py`

Keep raw reconciliation semantics.

Add only target-facing helpers if they depend directly on reconciliation outputs; otherwise keep them in `targets.py`.

### 22.4 `api.py`

Replace `build_groups` as the primary browser projection with reader-oriented target/outline builders.

File-only groups may remain temporarily for CLI/backward compatibility but must no longer drive browser progress.

### 22.5 Store schema

No new `review_targets` table in V1.

Targets are derived and deterministic.

Persisting them would create a second identity model that can drift from units.

Only add persistence later if profiling proves derivation cost material.

---

## 23. Frontend architecture

The current `ReviewWorkspace` is too responsible for unrelated concerns. Split the redesign around the reading loop.

Suggested structure:

```text
frontend/src/review/
  ReviewReader.tsx
  ReaderHeader.tsx
  ReviewOutline.tsx
  ReviewStream.tsx
  ReviewFile.tsx
  ReviewTargetHeader.tsx
  ReviewDiff.tsx
  InlineSignals.tsx
  ContextDrawer.tsx
    ImpactTab.tsx
    ChecksTab.tsx
    EvidenceTab.tsx
    AuthorTab.tsx
    DiscussionTab.tsx
  FinishReviewSheet.tsx
  RevisionDeltaSheet.tsx
  ReviewCommandPalette.tsx
  annotation/
    AnnotationCard.tsx
    Composer.tsx
  readerModel.ts
  reviewApi.ts
  types.ts
```

### 23.1 `ReviewReader`

Owns:

- session/revision identity;
- target order;
- focused target;
- search/filter;
- outline collapse;
- context open/tab/pin;
- revision-refresh lifecycle;
- keyboard routing.

It should not own detailed artifact rendering or diff internals.

### 23.2 `ReviewStream`

Owns:

- continuous target/file order;
- virtualization;
- scroll-to-target;
- current-target detection;
- preserving viewport anchor across mark/update operations.

### 23.3 Diff component

Continue using `@pierre/diffs` unless a concrete blocker appears.

Do not replace the diff renderer merely to implement the new IA.

Required additions:

- target-span decorations;
- multi-file stream mounting;
- line/range annotations;
- reviewed-target de-emphasis without reducing unread code readability;
- split/unified mode.

### 23.4 Context

Refactor existing useful `ContextPane` content into task tabs rather than discarding it.

Most logic is reusable; default visibility and hierarchy change.

---

## 24. Visual design rules

### 24.1 Density

This is an engineering tool. Favor density over marketing-style cards.

Use:

- thin separators;
- low-radius controls;
- compact typography;
- restrained status color;
- monospace where identity/location matters;
- sans-serif for explanations/comments.

### 24.2 Code remains highest contrast

Reviewed content may recede slightly, but never become difficult to re-read.

Avoid applying opacity to an entire file in a way that dims comments, line numbers, and code below comfortable readability.

Prefer subtle background/border treatment for completed targets.

### 24.3 Status color

Suggested semantic palette, respecting theme tokens rather than hard-coded product claims:

```text
amber   attention / uncertainty
sky     changed since review / informational movement
rose    failed check / needs changes
neutral ordinary / unreviewed
green   explicit human-reviewed / verification pass
```

Always pair color with text/glyph/state.

### 24.4 Radius

Keep component radii restrained and consistent. Review UI should feel like a technical instrument, not a card dashboard.

---

## 25. Focus mode

A senior reviewer must be able to remove almost all chrome.

`f` toggles focus mode:

```text
header: compact one-line
outline: collapsed rail/hidden
context: closed
footer: compact
code: maximum width
```

Current target state and keyboard navigation remain visible.

Focus mode is especially valuable for split diff on 13–15 inch laptop screens.

---

## 26. Performance requirements

Manual review flow must feel immediate.

### 26.1 Interaction budgets

Targets, measured locally after data is available:

```text
keyboard target navigation       < 50 ms UI response
mark reviewed optimistic focus   < 50 ms visual response
server mark round trip           < 250 ms typical local
open/close Context               < 100 ms
search filtering                 < 50 ms for 1,000 targets
scroll to unloaded target        < 300 ms typical local
initial useful reader            < 1.5 s on a 100-file review
```

The mark may show pending state immediately, but durable completion is confirmed only by the server.

### 26.2 Large-review qualification

Required test fixtures:

```text
10 files / 30 targets
100 files / 300 targets
500 files / 1,500 targets
large generated lockfile
single source file with >5,000 changed lines
```

No O(N full-patch-render) initial load.

### 26.3 Bundle size

Do not eagerly load heavy Evidence viewers or secondary tabs before Context is opened.

Code-split those sections where useful.

---

## 27. Accessibility

Required:

- every action reachable without mouse;
- every keyboard action also reachable with mouse/touch;
- visible focus ring;
- status not conveyed by color alone;
- target headers are navigable landmarks;
- drawers/sheets trap focus correctly;
- Escape behavior is deterministic;
- screen-reader labels use full paths/symbols rather than glyph only;
- line comment controls have accessible names;
- no shortcut fires while typing in input/textarea/contenteditable.

---

## 28. Degraded/uncertain operation

The reader must continue working when intelligence degrades.

### 28.1 No code index

Review still works.

Effects:

- lower-quality target ordering;
- caller counts may be unknown;
- some symbol targets may fall back to hunk targets.

The UI says this compactly. It does not block review.

### 28.2 Patch unavailable

Represent the file as a fallback file target with explicit refusal reason.

Do not show an empty diff that appears complete.

### 28.3 Symbol extraction ambiguity

Fall back to hunk target for the ambiguous changed span.

Do not pick one symbol merely to maintain a prettier hierarchy.

### 28.4 Unknown fingerprint

The target remains explicitly uncertain and cannot silently become durable reviewed state under existing semantics.

The finish sheet includes it as outstanding.

### 28.5 Provenance unavailable

Hide author-session actions that require exact provenance.

Do not show speculative author claims as fact.

---

## 29. CLI relationship

The browser and CLI remain projections of the same review state.

### 29.1 `lc review`

Terminal output remains useful for orientation:

```text
CHANGE STORY
ATTENTION
START HERE
EXECUTION EVIDENCE
REVIEW SESSION
```

But update counts to make the distinction explicit:

```text
files     39
targets   74
raw units 330
reviewed  52/74
```

Do not present raw units as the human workload.

### 29.2 `lc review --open`

Opening the browser automatically tracks the durable review as today.

### 29.3 CLI marking

Accept target labels/unit keys cleanly.

Where a path maps to multiple targets, a bare path should not silently mark the whole file. It should either:

- list targets; or
- require an explicit bulk-file flag/action.

### 29.4 Same vocabulary

The same target label must render consistently across terminal/browser feedback bundles.

---

## 30. What is removed from the current default UI

The redesign intentionally removes or demotes the following.

### 30.1 Remove permanent four-lens tabs

Current:

```text
Attention | Intent | Dependency | Commits
```

Replacement:

```text
one default recommended order
Order… menu for alternates
```

### 30.2 Remove permanent right Context pane

Context becomes a closed-by-default drawer.

### 30.3 Remove large Review Brief strip

Its information becomes compact orientation + expandable overview.

### 30.4 Remove duplicate “Major changes” and “Review first” strips

Review order and outline already express priority.

### 30.5 Remove file-only progress as primary completion

Progress is target-based.

### 30.6 Remove primary-header button crowding

Keep `Finish review` primary. Move refresh/feedback/mode controls into compact secondary locations.

### 30.7 Remove `looks_good` from new comment composer

Review state owns that judgment.

### 30.8 Avoid whole-file opacity as completion treatment

De-emphasize reviewed targets without degrading code readability.

---

## 31. What remains visibly first-class

These are not demoted:

- actual code diff;
- target judgment state;
- changed-since-review state;
- open human requests;
- failed/unknown verification;
- compact attention signals;
- exact revision identity;
- new-revision availability;
- explicit finish action.

---

## 32. Review lifecycle state machine

```text
                 ┌──────────────┐
                 │    OPEN      │
                 └──────┬───────┘
                        │ human finishes
                        ▼
                 ┌──────────────┐
                 │   FINISHED   │
                 └──────┬───────┘
                        │ reopen / accept newer revision
                        ▼
                 ┌──────────────┐
                 │    OPEN      │
                 └──────┬───────┘
                        │ explicit discard
                        ▼
                 ┌──────────────┐
                 │   ARCHIVED   │
                 │  read-only   │
                 └──────┬───────┘
                        │ restore
                        ▼
                      OPEN
```

Accepting a new revision of a finished review should require an explicit action and returns the review to `open`.

---

## 33. Target state machine across revisions

```text
unreviewed ──r──> reviewed
     │              │
     │ x            │ content changes
     ▼              ▼
needs_changes   changed_since_review
     │              │
     │ author edit  │ r after re-read
     │              ▼
     └──────────> reviewed

reviewed -- unit cannot be identified safely --> unknown / reopened according to reconciliation rule
```

Human request state is not erased by author intent.

---

## 34. Telemetry / dogfood metrics

For local/open-source use, metrics can remain local unless the user has explicitly enabled product analytics.

Track enough during dogfood to answer whether the redesign works.

### 34.1 Session metrics

```text
time_to_first_target_judgment
time_to_finish
context_opens
external_context_switches if observable only through explicit action
reviewed_targets
reopened_targets
bulk_reviewed_targets
comments_created
feedback_sent
revision_updates
preserved_judgments
reopened_judgments
orphaned_annotations
```

### 34.2 UX success metrics

Dogfood goals:

- reviewer can finish a meaningful 30+ target change without opening GitHub/IDE for basic navigation;
- median target review requires no Context open;
- revision 2 re-review time is materially lower than revision 1 when most code is unchanged;
- no user asks “what does reviewed percentage mean?”;
- no stale review mark survives changed content;
- no unchanged symbol target reopens because an unrelated file changed.

---

## 35. Test strategy

### 35.1 Unit tests — target derivation

Required cases:

1. one hunk inside one symbol → symbol target only;
2. two hunks in same symbol → one symbol target;
3. one hunk touches two sibling symbols → two symbol targets with non-overlapping changed spans;
4. nested definitions → deepest trustworthy owner;
5. config file without symbols → hunk target;
6. deletion-only hunk → hunk target;
7. ambiguous symbol extraction → hunk fallback;
8. binary → file target;
9. rename-only → file target;
10. mechanical file → mechanical targets;
11. symbol plus uncovered top-level change → symbol + hunk target;
12. no duplicate changed-line coverage;
13. every reviewable changed span has exactly one target or explicit file-level fallback.

### 35.2 Reconciliation tests

Required:

- reviewed symbol unchanged across new revision stays reviewed;
- reviewed symbol content changed becomes changed-since-review;
- unrelated file edit does not reopen symbol;
- file rename carries stable symbol judgment according to existing alias rules;
- hunk re-hunking resets only affected hunk targets;
- target derivation changes caused by better analysis never falsely preserves a judgment on different content;
- removed reviewed target produces discarded verdict history;
- author-addressed human comment remains human-unresolved.

### 35.3 API tests

Required:

- target endpoint uses latest frozen revision;
- mark accepts target unit key;
- mark response updates progress correctly;
- bulk endpoint refuses high-attention/changed/unknown targets;
- context never reads today's source instead of reviewed revision;
- refresh returns target-level delta;
- archived reviews remain read-only.

### 35.4 Frontend tests

Required:

- `j/k` walks targets;
- `J/K` walks file boundaries;
- `r` marks target and advances;
- rejected/downgraded mark does not advance silently;
- search constrains next-target navigation;
- Context opens correct tab from signal click;
- Escape closes Context/composer predictably;
- scroll establishes correct current target;
- target state update does not jump viewport;
- refresh preserves nearest surviving target;
- finish sheet counts target states, not files/raw units;
- comments remain mounted across same-file refetch;
- split/unified mode remains functional in stream.

### 35.5 Browser dogfood scenarios

At least:

1. small 5-file human-authored change;
2. 40-file agent change;
3. revision loop with three request-change comments;
4. second revision modifying only 20% of targets;
5. large generated lockfile plus small source change;
6. stale evidence + new revision;
7. exact provenance direct feedback to Claude;
8. provenance unknown;
9. degraded/no index;
10. orphaned annotation after structural rewrite.

---

## 36. Implementation plan

The redesign should be delivered in slices that preserve a usable review tool at every stage.

### Phase R20 — Target model

Backend only.

Deliver:

- `targets.py`;
- deterministic target derivation;
- target progress;
- target-level outline summary;
- tests;
- `GET /targets`.

Gate:

> For every fixture, changed text is covered exactly once by a review target/fallback, and progress cannot double-count overlapping file/hunk/symbol units.

### Phase R21 — Target-based marking

Deliver:

- browser API types;
- target mark flow;
- generalized bulk marking;
- CLI target counts;
- target-level progress.

Gate:

> `r` can mark a symbol/hunk target directly and a file with four targets no longer becomes reviewed from one ordinary file mark.

### Phase R22 — Continuous reader shell

Deliver:

- new compact header;
- compact outline;
- continuous multi-file stream;
- file + target headers;
- target keyboard navigation;
- focus mode;
- split/unified mode.

Keep current Context available temporarily if necessary behind drawer conversion.

Gate:

> Reviewer can complete a 30-target review without changing selected-file pages.

### Phase R23 — Context drawer

Deliver:

- drawer closed by default;
- Impact/Checks/Evidence/Author/Discussion tabs;
- inline signal → exact tab navigation;
- related-source peek preserving scroll position;
- optional wide-screen pin.

Gate:

> Opening Context never loses the reviewer's place and closing it restores full reading width immediately.

### Phase R24 — Comments + feedback polish

Deliver:

- target-aware comments;
- remove `looks_good` from new composer;
- request-change → target-state integration;
- author-addressed re-review state;
- finish/feedback flow integration.

Gate:

> A human can request changes, send them to an exact agent session, receive an author response, and retain ownership of resolution.

### Phase R25 — Revision-delta experience

Deliver:

- compact new-revision notification;
- target-level revision delta;
- preserved/reopened/new queues;
- best-target viewport restoration;
- discarded verdict presentation;
- hunk-reset explanation.

Gate:

> On a second revision where 80% of reviewed targets are unchanged, those 80% stay out of the active queue and the reviewer is shown only the 20% requiring judgment plus explicit unresolved comments/checks.

### Phase R26 — Finish review + cleanup

Deliver:

- finish sheet;
- target-based completion;
- outstanding-risk summary;
- header/action cleanup;
- remove legacy browser lens/group layout;
- remove permanent context pane;
- remove legacy review-brief strip.

Gate:

> A reviewer can explain every number in the finish sheet and no completion number is based on overlapping raw units.

### Phase R27 — Performance + accessibility qualification

Deliver:

- stream virtualization;
- patch prefetching;
- large review fixtures;
- interaction profiling;
- keyboard/a11y qualification;
- bundle code-splitting for secondary context features.

Gate:

> 500-file / 1,500-target fixture remains navigable without eager rendering of the entire patch corpus.

### Phase R28 — Dogfood release qualification

Use LemonCrow Review to review its own implementation from the previous stable baseline.

Record every moment where the reviewer:

- loses their place;
- cannot tell what is reviewed;
- leaves for another diff viewer;
- cannot locate impact evidence;
- mistrusts a carried judgment;
- cannot explain an outstanding count;
- cannot send actionable feedback without copy/paste;
- sees stale code/evidence represented as current.

Those are release blockers or explicitly documented follow-ups, not “UX polish later.”

---

## 37. Migration from current implementation

### 37.1 Reuse

Reuse:

- workspace authentication/bootstrap;
- review sessions/revisions/store;
- current patch synthesis;
- `@pierre/diffs` integration;
- annotation components/anchor semantics;
- evidence storage;
- related-source API;
- provenance projection;
- source-state probing;
- feedback export/delivery;
- finish/status persistence;
- existing unit derivation and reconciliation.

### 37.2 Refactor

Refactor:

- `ReviewWorkspace.tsx` → orchestration-focused `ReviewReader`;
- `AttentionPane` → `ReviewOutline`;
- `ContextPane` → drawer tabs;
- selected-file `DiffPane` → reusable file/target diff inside `ReviewStream`;
- `build_groups` → target/outline projection for browser.

### 37.3 Delete after parity

Delete browser-only legacy concepts after R26 parity:

- `ReviewLensTabs` as top-level navigation;
- file-only group progress UI;
- permanent context resize handle/default width storage;
- persistent Review Brief strip;
- Major changes pill row;
- Review first pill row;
- primary-header prepare-feedback button;
- primary-header refresh button where replaced by menu/new-revision action.

### 37.4 Backward compatibility

Keep old stored sessions readable.

Because targets are derived from units, existing sessions do not require a target migration.

If an old revision lacks enough metadata to derive modern targets, fall back conservatively to hunk/file targets and state the degraded mode.

---

## 38. Acceptance criteria — product

The redesign is complete only when all are true.

### Manual reading

### Manual reading

- [x] Opening a review puts real code on screen without requiring a mode choice.
- [x] The code owns the majority of viewport width by default.
- [x] A reviewer can read the entire changeset as one continuous stream.
- [x] A reviewer can navigate target-to-target and file-to-file without using the mouse.
- [x] Context is available without being permanently visible.

### Judgment precision

- [x] Review progress is based on non-overlapping `ReviewTarget`s.
- [x] Ordinary source files with multiple targets cannot be accidentally completed by one generic file mark.
- [x] Symbol judgments may span multiple hunks of the same symbol.
- [x] Uncovered changes fall back conservatively to hunk/file targets.
- [x] Unknown identity is explicit.

### Revision loop

- [x] Source changes do not mutate the revision currently being read.
- [x] Accepting a new revision reconciles marks before showing the new queue.
- [x] Unchanged reviewed targets stay reviewed.
- [x] Changed reviewed targets reopen.
- [x] New targets are visible as new.
- [x] Removed reviewed targets remain visible in revision history as discarded verdicts.
- [x] Re-hunked targets explain lost hunk judgments.

### Feedback

- [x] Inline/range comments remain durable across revisions when anchors resolve.
- [x] Heuristic relocation is distinguishable from exact anchoring.
- [x] Orphaned comments are never drawn on guessed lines.
- [x] Author “addressed” claims never auto-resolve human requests.
- [x] Exact-provenance feedback delivery is explicit and reviewable before send.

### Completion

- [x] Finish sheet counts targets, not files/raw units.
- [x] Failed/unknown verification is visible.
- [x] Changed-since-review work is visible.
- [x] Open/orphaned human comments are visible.
- [x] Human may still finish with outstanding work explicitly.

### Trust

- [x] No AI-safe-to-merge verdict exists.
- [x] No unknown signal is represented as zero/pass.
- [x] No stale artifact is represented as current.
- [x] No stale human mark survives changed target content silently.

All objective product criteria above are qualified on the current implementation. Section 39 remains the subjective human dogfood/release bar and is intentionally not converted into a mechanical pass.
---

## 39. Acceptance criteria — senior-engineer dogfood test

A senior engineer should be able to answer “yes” to these after reviewing a real agent change:

```text
Did I understand the shape of the change quickly?
Did I spend most of my time reading code rather than operating the tool?
Did the tool take me to the risky parts first without hiding the rest?
Could I inspect callers/tests/provenance when I needed them without losing my place?
Could I leave precise feedback without switching tools?
Could I tell exactly what I had and had not reviewed?
When the agent changed the code, did LC preserve the work I had already done?
Did LC put only the changed/new work back in front of me?
Could I explain every outstanding item before finishing?
Would I choose this over GitHub/IDE diff for the next agent-generated change?
```

The final question is the release bar.

---

## 40. The product in one sentence

> **LemonCrow is the manual code-review reader that remembers exactly what you already understood, shows what matters next, and makes you re-read only what actually changed.**

That is the product this implementation should now converge on.
