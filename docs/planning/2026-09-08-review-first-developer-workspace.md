# LemonCrow Review — Product, UX, and Execution Plan

> **Status:** historical baseline — superseded for current Review Reader UX by
> `docs/planning/2026-09-12-review-reader-spec.md`. The durable session/revision/
> annotation concepts below remain useful context; the selected-file/three-pane
> workspace descriptions do not define current product behavior.
>
> **Date:** 2026-09-08
> **Date:** 2026-09-08
>
> **Stage constraint:** very few users, fully open source, no commercialization dependency
>
> **Primary user:** an engineer who increasingly spends more time reviewing human- and agent-produced work than writing it
>
> **Core principle:** human attention is the scarce resource. LemonCrow compresses the amount of human attention required to understand each change, without outsourcing the engineering judgment.

---

## 1. Product decision

The agentic coding world creates a new bottleneck:

```text
agents can produce changes faster than humans can understand them
                         ↓
                 review becomes the work
```

LemonCrow should become a **review-first developer workspace** for both agent and human changes.

The user problem is best stated as:

> **I have agents writing a lot more code. Help me understand what they did.**

The product explanation is:

> **LemonCrow makes AI-written code easier for a human to understand, review, and operate — across whatever model or coding agent they use.**

The longer-term product vision is broader:

> **One place to understand and review everything humans and agents produce.**

Do not lead with the broad vision until the code-review workflow earns repeated use.

### What LemonCrow is not

LemonCrow is not another AI reviewer that reads a pull request and produces another page of prose for a human to review.

It should instead answer:

- what changed?
- what actually matters in this change?
- what else does this change affect?
- what did the authoring agent inspect?
- what did it not inspect?
- what tests/checks really ran?
- what have **I already reviewed**?
- what changed since I reviewed it?
- what feedback still needs action?
- where should that feedback go: back to an agent or to a human peer?

The scarce resource being optimized is **human attention**.

---

## 2. Why this category is emerging now

Several current products independently point at the same shift:

| Product | Current direction | Lesson for LemonCrow |
| --- | --- | --- |
| Hunk | review-first terminal diff viewer; live agent sessions; extensible viewer | better diff ergonomics matter, but the viewer alone is not LemonCrow's moat |
| Pyor | human-attention-first PR review; important files first; AI points instead of writing | attention triage is becoming a product category |
| Plannotator | code/plan/doc review; annotations flow back to agents | review and guidance should be one loop |
| Reviewable | durable multi-revision review state; agents now participate through MCP/CLI | revision-aware review state is more important as agents iterate quickly |
| Graphite | modern PR surface, inbox, stacked PRs, AI review | team PR workflow is valuable, but competing with GitHub end-to-end is too large for LemonCrow now |

References checked on 2026-09-08:

- <https://www.hunk.dev/>
- <https://pyor.review/>
- <https://plannotator.ai/>
- <https://www.reviewable.io/blog/we-built-an-mcp-server/>
- <https://www.graphite.com/>

The category distinction that matters:

```text
Generation 1: GitHub
  diff -> comment -> approve

Generation 2: AI reviewer
  diff -> AI produces findings -> human reviews diff + findings

Generation 3: review-first agentic development
  change -> organize human attention -> human reviews -> feedback -> human or agent revises
                                                ↑                         |
                                                └──── review delta ──────┘
```

LemonCrow should build for Generation 3.

---

## 3. The fundamental abstraction: Review Session

The durable product object should **not** be a pull request and should **not** be an agent session.

It should be a `ReviewSession`.

A review session says:

```text
What am I reviewing?
What revision am I looking at?
What have I already understood/approved?
What changed since then?
What needs my attention now?
What comments/requests are unresolved?
What evidence explains this change?
Where should feedback be delivered?
```

The same abstraction must support:

```text
local agent working-tree change
local commit / branch
peer GitHub PR
later: GitLab PR/MR
Markdown plan / RFC / ADR / README
agent-generated document
human-generated document
```

The **source changes**. The review interaction should not.

### 3.1 Subject types

```python
ReviewSubjectType = Literal[
    "local_change",
    "commit_range",
    "pull_request",
    "document",
]
```

Do not create separate review systems for agent code, peer code, and documents.

### 3.2 Actor types

```python
ActorType = Literal[
    "human",
    "agent",
    "mixed",
    "unknown",
]
```

Authorship is provenance, not a separate workflow.

A peer PR may itself contain agent-generated commits. A local change may have human edits mixed into agent work. The review system should represent this without forcing a binary choice.

---

## 4. Product principles

### 4.1 Human review is the final authority

LemonCrow may produce evidence, ordering, and attention signals.

It must not turn them into:

```text
AI APPROVED
SAFE TO MERGE
```

unless those are objective policy/check results with explicit semantics.

The review surface ends in human state such as:

```text
Unreviewed
Needs changes
Reviewed
Approved by Pankaj
```

### 4.2 AI should point before it writes

Default LemonCrow intelligence should be deterministic where possible:

- semantic changed symbols;
- callers and impact outside the patch;
- public/signature/contract changes;
- review ordering;
- test/check evidence;
- provenance;
- mechanical/generated change classification;
- changed-since-review detection.

Optional model assistance may explain a selected hunk or answer a reviewer question **on demand**.

Do not automatically generate a 1,500-word review summary.

### 4.3 Review state is more valuable than another summary

The product should remember:

- which file/hunk/section the reviewer has reviewed;
- which revision it was reviewed against;
- which comments were made;
- which comments were addressed;
- which content changed afterward;
- which annotations no longer anchor cleanly.

This is what lets the reviewer safely resume after an agent makes another large iteration.

### 4.4 Unknown is a first-class state

Carry the trust posture already present in `lc review`:

```text
unknown != zero
possible != exact
not observed != did not happen
```

Never silently move a comment to a different line after a revision. Never claim a test passed because a test-shaped command existed. Never attribute a change to an agent without evidence.

### 4.5 Source-agnostic, delivery-aware

The reviewer should use the same comment interaction regardless of who authored the work.

Only delivery differs:

```text
local agent change -> structured feedback back into agent session
peer PR            -> GitHub review/comment
local document     -> feedback bundle / agent session
shared document    -> later adapter
```

---

## 5. The core UX

The first interactive product should be a **local review workspace**, opened from the CLI.

```text
lc review --open
```

The current command already builds a strong review packet and static HTML report. Evolve `--open` into a persistent local review application rather than creating a separate product command.

### 5.1 Workspace layout

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ Checkout refactor                 rev 4     67% reviewed     Send feedback│
│ Agent: Claude Code / Opus 5       14 files  +384 -211        Finish review│
├─────────────────────┬───────────────────────────────────────┬─────────────┤
│ ATTENTION / FILES   │ CHANGE                                │ CONTEXT     │
│                     │                                       │             │
│ Needs attention  3  │ auth/session.py                       │ Impact      │
│ Changed since me 2  │                                       │ 7 callers   │
│ Unreviewed       4  │ @@ ...                                │ 2 outside   │
│ Reviewed         5  │ - old                                 │             │
│ Mechanical      3   │ + new                                 │ Provenance  │
│                     │                                       │ inspected ✓ │
│ > auth/session.py   │ [comment marker]                      │ missed ⚠    │
│   api/login.py      │                                       │             │
│   tests/...         │                                       │ Evidence    │
│                     │                                       │ tests PASS  │
├─────────────────────┴───────────────────────────────────────┴─────────────┤
│ c comment   r reviewed   j/k navigate   ] next attention   ? context     │
└──────────────────────────────────────────────────────────────────────────┘
```

The right context pane/drawer must be quiet by default. It should show evidence relevant to the selected file/hunk, not the entire repository report.

### 5.2 Left pane: human attention, not alphabetical files

Top-level groups:

1. **Needs attention**
2. **Changed since my review**
3. **Unreviewed**
4. **Reviewed**
5. **Mechanical / generated**

Within each group, use LemonCrow's existing review ordering.

Every ranked item needs an explanation:

```text
auth/session.py
  7 callers · public signature changed · 2 impacted sites outside patch
```

No unexplained AI score.

### 5.3 Center pane: real diff/document

For code:

- split/unified mode;
- syntax highlighting;
- file tree;
- line numbers;
- hunk navigation;
- collapse generated/mechanical regions;
- show only changed-since-review when requested;
- inline annotation markers;
- comment on line/range/file.

For documents later:

- rendered Markdown + source toggle;
- section navigation;
- select text/range and annotate;
- revision delta between reviewed version and latest.

### 5.4 Right pane: evidence, not another review essay

For selected code:

```text
CHANGE IMPACT
refresh() signature changed
7 known callers
2 callers outside this patch

AGENT PROVENANCE
Claude Code · Opus 5 · exact session anchor
inspected this file ✓
jobs/session_cleanup.py affected but not inspected ⚠

VERIFICATION
Focused tests      PASS
Full suite         NOT_RUN
Typecheck          PASS

WHY THIS IS FIRST
public contract changed
2 out-of-patch impacts
high-centrality symbol
```

For human-authored code, agent provenance simply disappears or says mixed/unknown where appropriate.

### 5.5 Review actions

The smallest useful actions are:

```text
Comment
Request change
Suggestion
Looks good
Mark reviewed
Reopen
Resolve
Send feedback
Finish review
```

Do not build dozens of review disposition types initially.

### 5.6 Visual proof is first-class review evidence

For UI, mobile, visual, and interaction-heavy changes, the code diff is often not the fastest way to understand the outcome. LemonCrow should support **review evidence** attached to a specific `ReviewRevision`.

Do not model this as a generic file attachment bucket. An evidence item should say what it proves, which revision produced it, and whether it is still current.

Initial evidence kinds:

```python
ReviewEvidenceKind = Literal[
    "screenshot",
    "visual_diff",
    "playwright_trace",
    "video",
    "live_preview",
    "image",
    "document",
]
```

A review could therefore look like:

```text
Checkout redesign                         rev 6

CODE       OUTCOME       EVIDENCE

Visual evidence
  checkout-desktop.png       screenshot       UNREVIEWED
  checkout-mobile.png        screenshot       REVIEWED
  checkout-before-after      visual diff      CHANGED
  checkout-flow.trace.zip    interaction      UNREVIEWED
  preview                    live preview     OPEN

Code evidence
  Focused tests              PASS
  Typecheck                  PASS
  Full suite                 NOT_RUN
```

The key rule is **revision binding**:

```text
artifact captured for rev 5 + code changes in rev 6
    -> evidence becomes STALE until explicitly regenerated or confirmed

artifact content unchanged and its producing inputs are unchanged
    -> reviewed state may persist
```

Do not let an old screenshot continue to visually “prove” a newer revision.

#### Screenshots first

Screenshots are the default visual proof because they are:

- cheap to capture;
- easy to compare;
- easy to annotate;
- easy to persist locally;
- easy to show beside a code change;
- much faster to skim than video.

Support baseline/head/diff viewing when a baseline exists:

```text
BASELINE        LATEST        DIFF
```

Reuse existing test/CI screenshots when present. For new capture support, prefer Playwright rather than building browser automation.

#### Interaction traces before raw video

For flows such as checkout, drag-and-drop, forms, menus, animation-triggered behavior, and multi-step navigation, a plain screenshot loses too much information.

Prefer a Playwright trace as the primary interaction artifact because it can preserve:

- action timeline;
- screenshots/filmstrip;
- before/after DOM snapshots;
- network activity;
- console output;
- source/action timing.

A reviewer can inspect the exact step where behavior diverged rather than scrub an opaque recording.

Raw video remains useful when **motion itself is the thing being reviewed**:

- animation/easing;
- gesture behavior;
- scrolling;
- transitions;
- latency/perceived responsiveness;
- media playback.

Keep video optional and short. Never require agents to record videos for ordinary backend or static code changes.

#### Live preview is a reference, not a new runtime business

A review may carry a `live_preview` URL or local target, but LemonCrow should initially **consume** previews rather than own preview infrastructure.

Examples:

```text
localhost dev server
Vercel preview deployment
existing Storybook
existing mobile/emulator URL
external sandbox URL
```

Do not build ShipIt-style Docker orchestration, preview environments, deployment hosting, or tunneling just to support this feature. If a preview already exists, surface it in the review workspace and bind comments/evidence to the revision.

#### Visual comments

Allow comments on screenshot coordinates/regions. Store normalized coordinates plus artifact hash, never only viewport pixels.

Later, for live previews, consider DOM-aware anchors such as URL + selector + captured screenshot, but do not make DOM anchoring a Phase-1 dependency.

#### Artifact gallery

Borrow the useful part of ShipIt's `Present` concept: one small **Evidence** gallery per review revision for screenshots, traces, videos, diagrams, and other proof produced by humans, agents, tests, or CI.

The gallery is not a general artifact management product. Its purpose is answering:

> **What observable result proves this change behaves the way the author claims?**

---

## 6. The killer feature: “what changed since I reviewed?”

GitHub mostly asks:

> what changed since base/main?

The agentic reviewer needs:

> **what changed since I last reviewed this?**

This is the core revision model.

### 6.1 Review marks belong to content, not line numbers

A reviewer mark should be anchored to a stable review unit and revision fingerprint.

Suggested unit kinds:

```python
ReviewUnitKind = Literal[
    "file",
    "symbol",
    "hunk",
    "document_section",
]
```

Each unit carries a semantic/content fingerprint.

### 6.2 Revision update behavior

When a new revision arrives:

```text
unchanged reviewed unit
    -> remains reviewed

changed reviewed unit
    -> CHANGED SINCE REVIEW

new unit
    -> UNREVIEWED

removed unit
    -> archived/resolved from active review

comment anchor still exact
    -> carry forward

comment anchor moves uniquely
    -> carry forward with relocated marker

comment anchor ambiguous
    -> ORPHANED / needs re-anchor
```

Never silently preserve `reviewed` across changed content.

### 6.3 Review frontier

Maintain a simple per-reviewer `ReviewFrontier`:

```python
ReviewFrontier(
    review_id,
    reviewer_id,
    last_seen_revision,
    reviewed_unit_fingerprints,
    unresolved_annotation_ids,
)
```

This is more important than storing a generic “approved PR” bit.

---

## 7. Robust annotation anchoring

Line numbers are not durable enough when agents rewrite files repeatedly.

Every annotation should store multiple anchors:

```python
AnnotationAnchor(
    path,
    side,                    # old | new | document
    start_line,
    end_line,
    selected_text_hash,
    before_context_hash,
    after_context_hash,
    symbol_qualified_name,
    symbol_fingerprint,
    blob_sha,
)
```

Resolution order on a new revision:

1. identical blob + line range;
2. exact selected text with matching context;
3. same symbol + selected text;
4. unique selected-text occurrence;
5. unique context match;
6. otherwise mark the annotation orphaned.

If more than one location fits, **do not guess**.

This trust behavior should match LemonCrow's current conservative provenance/impact implementation.

---

## 8. Attention engine

The current `ReviewPacket` already provides much of the initial attention engine:

- changed files/hunks;
- changed symbols;
- semantic impact sites;
- untouched callers;
- centrality;
- file categories;
- provenance;
- verification evidence;
- deterministic review ordering.

Do not replace this with an LLM ranker.

### 8.1 Initial deterministic attention rules

High attention:

```text
public/signature contract changed
removed symbol with surviving references
out-of-patch impact
failed verification
migration/config contract change
reviewed unit changed after reviewer mark
unresolved reviewer comment
```

Medium attention:

```text
high-centrality changed symbol
many known callers
production behavior change
agent did not inspect an impacted file
unknown verification for a risky change
```

Low attention / skim:

```text
generated output
lockfile-only change
formatting-only change
mechanical rename with verified references
snapshot/golden update when source change is already reviewed
```

Every signal must carry a reason and evidence reference.

### 8.2 Reviewer overrides teach preference, not truth

Let the reviewer say:

```text
not useful
important
always show this kind
usually collapse this path
```

Do not immediately build an ML preference model. Store the signals as review history first.

Later LemonCrow's existing lesson machinery can distill repeated preferences into explicit local/team review rules.

---

## 9. Provenance becomes first-class review context

Current `lc review` often has strong change impact but may still report provenance as unknown. The next provenance work should improve **capture**, not heuristic matching.

When a supported agent works in a LemonCrow-integrated repository, record exact anchors:

```text
session id
host
model
workspace
base commit
starting tree state
files read
files edited
commands run
checks/tests and exit codes
resulting commit SHA when committed
working-tree snapshot/revision id when uncommitted
```

Then the review can answer:

```text
what the agent saw
what it changed
what it tested
what it did not inspect despite semantic impact
```

That is the concrete meaning of:

> Help me understand what the agent did.

### 9.1 Mixed authorship

Do not assume one agent owns an entire PR.

At revision/commit level preserve authoring provenance where known:

```text
rev 1  Claude Code
rev 2  human edit
rev 3  Codex
rev 4  human fix
```

The reviewer can still use one review workspace.

---

## 10. Feedback loop: same annotation, different recipient

The core annotation model should not know whether the recipient is human or agent.

A separate delivery adapter does.

```python
FeedbackTargetType = Literal[
    "agent_session",
    "github_review",
    "clipboard",
    "markdown_bundle",
]
```

### 10.1 Local agent feedback

Initial low-complexity flow:

```text
review -> annotate -> Send feedback
                     ↓
structured Markdown bundle
                     ↓
existing agent session / resume-context / clipboard
```

Example bundle:

```markdown
## Review feedback

### src/auth/session.py:L81-L88 — request change
`refresh()` gained a required argument, but `jobs/session_cleanup.py` still calls the old form.

### src/api/login.py:L41 — comment
Please keep this branch explicit; the fallback hides invalid state.

### Review context
- impacted caller: jobs/session_cleanup.py:L82
- focused tests: PASS
- full suite: NOT_RUN
```

Do this before building host-specific live steering for every agent.

### 10.2 Later host adapters

Once repeated local usage is proven:

```text
Claude Code adapter
Codex adapter
OpenCode/LemonCode adapter
other hosts only when demand exists
```

The adapter should deliver the same feedback object; it must not create host-specific review semantics.

### 10.3 Peer PR feedback

Use GitHub as a transport/source of truth, not the primary review UX.

Later:

```text
lc review pr https://github.com/org/repo/pull/123 --open
```

Fetch via `gh`/GitHub API, review locally, and explicitly publish selected comments/review state back to GitHub.

Do not require a LemonCrow cloud account for this.

---

## 11. Review Inbox — later, but design the data model for it now

Once one person is reviewing several agent and peer changes, the next natural surface is an inbox:

```text
REVIEW INBOX

Needs me
  checkout refactor       Agent/Claude       3 changed since review
  PR #812 auth cleanup    Sarah               7 unreviewed
  migration plan          Agent/Codex         2 comments addressed

Waiting
  sizing tests            Agent/OpenCode      working
  PR #799 API cleanup     Thom                awaiting update

Done today
  ...
```

This should aggregate `ReviewSession`s. It should not become a separate workflow engine.

Do not build the inbox before individual review sessions are sticky.

---

## 12. Documents and plans

The quoted developer workflow includes docs produced by humans or machines. That is a natural extension **after code review is validated** because the same core interaction applies:

```text
open revision
read
annotate
mark reviewed
send feedback
receive revision
see only changed-since-review
```

Initial document type: Markdown only.

Do not build Google Docs/Notion/Confluence editors.

Use source files/URLs as inputs and keep external systems as sources of truth.

Potential command:

```text
lc review doc docs/architecture/auth.md --open
```

Plans generated by coding agents can use the same surface.

---

## 13. Architecture

### 13.1 Reuse current LemonCrow instead of creating another stack

Already built and reusable:

```text
review/gitdiff.py       raw diff + revision handling
review/impact.py        semantic change impact
review/ordering.py      human review order
review/provenance.py    agent/session evidence
review/models.py        ReviewPacket
review/html.py          existing browser rendering
code graph              symbols/callers/centrality/history
run ledger              execution events + git anchors
usage                    model/token/cost provenance
resume-context           bounded task continuation
lesson machinery         future explicit review preferences
```

Do not rebuild these.

### 13.2 New core package

Keep review intelligence in `review/`; add durable human-review state beside it.

Suggested modules:

```text
src/lemoncrow/pro/capabilities/review/
    models.py                 existing machine-readable review packet
    gitdiff.py                existing
    impact.py                 existing
    ordering.py               existing
    provenance.py             existing

    session_models.py         ReviewSession / Revision / marks / annotations
    store.py                  local SQLite persistence
    anchors.py                LemonCrow policy over OSS text/symbol remapping
    revisions.py              changed-since-review reconciliation
    feedback.py               structured feedback bundles
    sources/
        local.py
        github.py             later
        document.py           later
```

### 13.2.1 OSS-first implementation rule

LemonCrow should **not** implement commodity review infrastructure merely because it is convenient to keep everything in one repository.

The rule is:

> **Build the review semantics and intelligence that differentiate LemonCrow. Reuse mature open-source components for rendering, parsing, transport, storage plumbing, syntax, and text mapping.**

Prefer permissive Apache-2.0/MIT/BSD dependencies for core/distributed surfaces unless there is a deliberate reason to accept a stronger license.

#### Reuse matrix

| Need | Reuse | LemonCrow-specific code |
| --- | --- | --- |
| Git revision/diff access | existing `pygit2` / libgit2 | range semantics and `ReviewPacket` projection |
| syntax/symbol structure | existing tree-sitter + `tree-sitter-language-pack` | semantic impact and review-unit identity |
| fuzzy text relocation | existing `diff-match-patch` package | conservative anchor-resolution policy and ambiguity handling |
| browser diff rendering | `@pierre/diffs` | attention ordering, review marks, annotations, context pane |
| syntax highlighting/themes | Shiki through `@pierre/diffs` | none beyond product styling |
| browser app shell | existing `frontend/` React + Vite + Tailwind | review workspace layout and review state interactions |
| Markdown rendering | existing `react-markdown` | review-unit/section mapping and annotations |
| local HTTP/API | existing FastAPI + Uvicorn | review endpoints/auth token and domain objects |
| persistence | SQLite using existing Python/runtime helpers or stdlib `sqlite3` | review schema and migrations only |
| GitHub auth/transport | `gh` CLI first; GitHub REST only when needed | mapping ReviewSession comments/marks to GitHub semantics |
| terminal diff rendering | existing LemonCode viewer or optional Hunk `HunkDiffView` / `HunkReviewStream` | LemonCrow evidence panes and state bridge |
| IDs/hashes | stdlib UUID + existing BLAKE3 utilities | unit/content fingerprint composition |

`diff-match-patch` is already a LemonCrow dependency. Use it as a **candidate locator**, not as the authority: a fuzzy text match only survives when LemonCrow's path/symbol/context checks leave one unambiguous target. Do not replace the current conservative trust model with best-effort patch application.

For the browser, prefer adding `@pierre/diffs` to the existing React frontend rather than copying the LemonCode TUI renderer or writing another HTML diff implementation. It already provides React components, split/stacked layouts, line selection, syntax highlighting, and an annotation extension surface.

For GitHub, use the user's existing `gh` authentication initially. Do not build OAuth, token storage, webhook infrastructure, or a custom GitHub SDK layer merely to validate peer review.

#### Things we explicitly do not build ourselves

```text
generic diff algorithm
generic diff renderer
syntax highlighter
Markdown renderer
GitHub authentication client
fuzzy text diff/match algorithm
HTTP framework
SQLite engine / ORM abstraction
terminal rendering engine
```

The custom code should concentrate on:

```text
ReviewSession / ReviewRevision semantics
changed-since-my-review frontier
content/symbol review-unit identity
conservative comment re-anchoring policy
semantic impact / attention reasons
agent provenance and verification evidence
human review marks and feedback lifecycle
human/agent delivery abstraction
```

That is the part competitors and commodity libraries do not give LemonCrow for free.

### 13.3 Local review service
```text
GET  /api/reviews/:id
GET  /api/reviews/:id/revisions
POST /api/reviews/:id/marks
POST /api/reviews/:id/annotations
PATCH /api/annotations/:id
POST /api/reviews/:id/feedback/export
POST /api/reviews/:id/refresh
```

Bind loopback only by default.

Use a random per-process token in the browser URL/header to avoid other local pages driving the review service.

### 13.4 Storage

Default local store:

```text
~/.lemoncrow/reviews.sqlite
~/.lemoncrow/review-artifacts/<review-id>/...
```

Persist:

- review metadata;
- revision metadata;
- review packet snapshot;
- patch/document snapshot needed for uncommitted revisions;
- review marks;
- annotations;
- delivery state;
- anchor resolution history.

Do not persist secrets or full unrelated repository content.

### 13.5 Minimal schema

```text
review_sessions
  id
  subject_type
  title
  repo_root
  source_ref
  actor_type
  status
  created_at
  updated_at

review_revisions
  id
  review_id
  revision_number
  source_sha
  tree_fingerprint
  packet_artifact
  created_at

review_units
  revision_id
  unit_key
  kind
  path
  symbol
  content_fingerprint

review_marks
  review_id
  reviewer_id
  unit_key
  reviewed_revision_id
  content_fingerprint
  state
  updated_at

annotations
  id
  review_id
  revision_id
  anchor_json
  kind
  body
  state
  created_by
  created_at

review_evidence
  id
  review_id
  revision_id
  kind
  title
  artifact_path_or_url
  content_hash
  source                 # human | agent | test | ci | external
  source_ref
  status                 # current | stale | missing
  metadata_json
  created_at

review_evidence_marks
  evidence_id
  reviewer_id
  reviewed_content_hash
  state
  updated_at

deliveries
  id
  annotation_id
  target_type
  target_ref
  state
  remote_ref
  last_error
```
## 14. Frontend choice

### 14.1 Do not build a generic diff engine from scratch

There are four useful assets:

1. current LemonCrow static HTML review renderer — keep it as a lightweight/export surface, but do not evolve it into the interactive diff engine;
2. `@pierre/diffs` — Apache-2.0, React/vanilla, Shiki-backed split/stacked diff rendering with line selection and annotations;
3. LemonCode's existing TUI diff viewer with file tree, split/unified views, navigation, and reviewed-file state;
4. Hunk's open-source extension API and embeddable OpenTUI review components.

For the first sticky product, own the **review state and intelligence**, not the rendering technology.

### 14.2 Recommended browser path

Use the existing `frontend/` React/Vite application and add a local-only review route/shell. Do not create another frontend package.

The center diff pane should use `@pierre/diffs` directly. Let it own:

```text
patch parsing/rendering
split vs stacked layout
syntax highlighting via Shiki
line numbers/wrapping
line/range selection
annotation render slots
```

LemonCrow owns:

```text
which files/units appear first
changed-since-review state
reviewed/unreviewed state
annotation persistence and semantics
impact/provenance/verification side pane
feedback delivery
```

Feed the viewer from the existing `ReviewPacket` plus durable review-session state. Do not duplicate diff data models unless an adapter is required at the UI boundary.

Use existing `react-markdown` for document/plan review later. Do not build Markdown rendering or editing infrastructure.

LemonCode's reviewed-state interaction remains a useful keyboard/UX reference, but its current reviewed set is ephemeral and resets with the viewer. Durable state must live in LemonCrow core so browser/TUI/Hunk integrations can share it.

### 14.3 Hunk integration / terminal reuse

Hunk should be treated in two possible ways:

1. **distribution experiment:** a LemonCrow Hunk extension calls `lc review --json` and adds review ordering/evidence/annotations;
2. **terminal rendering reuse:** if LemonCrow needs its own terminal review surface, embed Hunk's exported OpenTUI components rather than implementing another terminal diff renderer.

Conceptual extension flow:

```text
Hunk renders the diff
      +
LemonCrow supplies:
  attention ordering
  semantic impact
  provenance
  verification evidence
  durable review state bridge
```

Do this only when it stays thin. Do not make the core review model dependent on Hunk, and do not delay persistent ReviewSession work for it.
```text
hunk extension -> lc review --json -> side pane / annotations
```

Do this only if it is a small integration. Do not make LemonCrow dependent on Hunk or delay the persistent review-session work for it.

---

## 15. CLI design

Preserve current behavior:

```text
lc review [REV]
```

It remains a useful terminal summary.

Evolve it into an invoke-without-subcommand group later without breaking that form:

```text
lc review [REV] --open              # create/open persistent review session
lc review --working-tree --open
lc review --staged --open

lc review inbox                     # later
lc review show <review-id>
lc review refresh <review-id>
lc review feedback <review-id>      # export structured feedback
lc review pr <url> --open           # later
lc review doc <path> --open         # later
```

Do not create a second top-level `review-app` or `workspace` command.

---

## 16. Agent-facing protocol

Agents should be able to receive feedback and report a new revision without understanding the UI.

Longer-term MCP/CLI primitives:

```text
review.get_feedback(review_id)
review.report_revision(review_id, revision metadata)
review.get_open_comments(review_id)
```

But do not begin with a broad MCP API.

Start with a deterministic feedback export that existing host integrations can consume.

The review system owns annotations. Agents are clients of that state, not authors of hidden review state.

---

## 17. Open-source product strategy

At this stage all core review capability should remain open source and local-first.

That includes:

- review packet/intelligence;
- review session persistence;
- browser workspace;
- annotations;
- changed-since-review state;
- local agent feedback;
- GitHub adapter if built.

Do not distort architecture now to manufacture a closed-source moat.

If commercial demand later appears, natural hosted conveniences include:

```text
shared team review inbox
review-state sync across machines
organization review history
SSO/permissions
team metrics
managed GitHub/GitLab integration
shared review rules
```

Those are service conveniences around the open review model, not reasons to cripple local OSS.

---

## 18. Execution plan

The product already has `lc review`, `lc usage`, `lc model`, run attribution, and resume context. Do **not** add another broad runtime roadmap in parallel.

### Phase 0 — Correct trust gaps and reposition (small)

Before interactive review work:

1. Fix `lc context doctor` so `LOADED`/recoverable-token claims reflect actual host loading semantics or are renamed to defined/configured size.
2. Improve exact provenance capture at authoring time for supported host integrations; do not grow heuristic matching indefinitely.
3. Change README, landing page, and CLI one-line description to the review-first product story.
4. Keep `lc usage` and `lc model` as supporting surfaces; no feature expansion unless users request it.

**Gate:** current `lc review` findings are trusted by internal use on real changes.

### Phase 1 — Persistent local review session

Build only:

- `ReviewSession` + `ReviewRevision` models;
- SQLite store;
- persistent per-file/per-hunk reviewed state;
- annotation model;
- interactive local browser workspace;
- current ReviewPacket evidence in context pane;
- `lc review --open` creates/reopens the session.

Do not add GitHub writeback or cloud accounts.

**Acceptance:** stop/reopen the browser and every review mark/comment survives.

### Phase 2 — Revision delta + review frontier

Build:

- working-tree refresh/watch;
- new revision snapshots;
- content fingerprints;
- anchor reconciliation;
- `Changed since my review` group;
- unchanged review marks persist;
- changed marks reopen;
- orphaned comments are explicit.

This is the first major differentiator over ordinary diff viewers.

**Acceptance:** an agent rewrites a previously reviewed 20-file change; LemonCrow surfaces only the units that actually require human re-review while preserving valid comments/marks.

### Phase 2.5 — Outcome evidence (see §25)

Do this as a narrow experiment, not a new preview platform.

Evidence is **reviewable state**, not a PR attachment: it carries the same `reviewed` / `changed since my review` / `unreviewed` semantics as a code unit, and it is bound to a `ReviewRevision`. Full model in §25.

Build:

- `ReviewEvidence` records bound to a `ReviewRevision`, with evidence review marks;
- ingest of artifacts that already exist — Playwright `trace.zip`, test/CI screenshot output, Storybook/Chromatic/Argos assets, agent-produced images, a preview URL;
- the OUTCOME pane: screenshots first, `BASELINE | NEW | DIFF` when a baseline exists;
- evidence↔code binding: which changed units produced this evidence, and which of them the agent never inspected;
- Playwright trace opened in Playwright's own viewer;
- video only for time/motion evidence;
- stale evidence when a newer revision exists.

Prefer consuming artifacts from existing tests, agents, CI, Storybook, Playwright, Vercel, or other preview systems. Do not build environment orchestration, a capture harness, a visual-diff algorithm, or a trace viewer.

**Gate:** UI-heavy design partners say visual proof materially reduces the need to run the branch manually before reviewing it.

### Phase 3 — Feedback back to local agents

Build:

- structured feedback bundle;
- `Send feedback`;
- clipboard/export baseline;
- adapter for the one host used most in validation;
- new revision returns to the same review session.

Do not implement every host simultaneously.

**Acceptance:** review -> comment -> send -> agent revises -> reviewer sees changed-since-review, without manually reconstructing a prompt.

### Phase 4 — GitHub peer review adapter

Only after local review is repeatedly used.

Build:

- open PR by URL/number;
- fetch diff/revisions/comments;
- create local ReviewSession;
- local review marks remain local initially;
- explicit `Publish` pushes selected comments/review state to GitHub;
- refresh PR and preserve LemonCrow review frontier.

Use `gh`/GitHub API. Do not build repository hosting or merge queues.

**Acceptance:** a user chooses LemonCrow over github.com for reviewing at least one real peer PR end-to-end.

### Phase 5 — Markdown/doc review

Only after code workflow is sticky.

Build:

- Markdown subject adapter;
- section-level review units;
- text-range annotations;
- revision delta;
- same feedback delivery.

**Acceptance:** the same reviewer uses LemonCrow for a plan/RFC without learning a different workflow.

### Phase 6 — Inbox/team sync only after pull

Possible future work:

- review inbox;
- shared/team review sessions;
- sync service;
- organization identity/permissions;
- review throughput metrics.

Do not build this because competitors have dashboards. Build only when multiple developers are already asking to share LemonCrow review state.

---

## 19. PR sequence

Keep implementation slices narrow and independently usable.

### PR-R1 — Review session schema + local store

- session/revision/unit/mark/annotation contracts
- SQLite persistence using existing runtime DB helpers / stdlib `sqlite3`; do not introduce an ORM for this slice
- migration/versioning tests
- no UI yet

### PR-R2 — Persistent reviewed state

- derive review units from current packet
- mark file/hunk reviewed
- reopen session
- content fingerprints using existing hash utilities

### PR-R3 — Interactive local workspace

- reuse existing FastAPI/Uvicorn for the loopback review API
- add review route to existing `frontend/` React/Vite app; no second frontend
- use `@pierre/diffs` for the center diff pane
- use existing Shiki integration through the diff library
- file tree / LemonCrow review ordering
- context/evidence drawer
- reviewed-state interaction

### PR-R4 — Annotations

- use `@pierre/diffs` line/range selection and annotation extension points in the viewer
- line/range/file comments
- request-change / comment / suggestion / looks-good
- persistent threads in LemonCrow state
- annotation markers supplied from persisted state

### PR-R5 — Revision reconciliation

- refresh source
- new revision
- use tree-sitter symbol identity + existing `diff-match-patch` as candidate relocation primitives
- LemonCrow applies conservative unique-match policy; ambiguity becomes orphaned
- changed-since-review
- explicit orphaned anchors

### PR-R5V — Outcome evidence (§25)

Split like PR-R3, OSS gate first:

**PR-R5Va — evidence model (backend, no UI).** `ReviewEvidence` + evidence review marks sharing the code-unit state machine; ingest from Playwright `trace.zip`, an artifact directory, a preview URL, or an explicit path; evidence fingerprint + revision-stale semantics; evidence↔unit binding and the agent-inspection gap; `lc review evidence add|list`.

**PR-R5Vb — OUTCOME pane (UI).** Third dimension beside CODE and CONTEXT; screenshots first with `BASELINE | NEW | DIFF` when a baseline exists; `Open trace ↗` to Playwright's viewer; `Open preview ↗`; video only for motion; region comments only if the adopted diff/annotation library makes them nearly free.

Do not build: capture harness, preview/container orchestration, visual-diff algorithm, trace viewer, video player, screenshot storage service.

Only keep this slice if UI-heavy users actually use it during reviews.

### PR-R6 — Structured feedback export

- selected/open annotations -> deterministic Markdown/JSON
- evidence references
- delivery state

### PR-R7 — One local agent delivery adapter

Choose based on actual validation usage, not market share guesses.

### PR-R8 — GitHub read adapter

- use `gh` CLI authentication and API access first
- PR source
- revisions
- author metadata
- existing comments read-only
- do not add OAuth/token-storage infrastructure

### PR-R9 — GitHub publish adapter

- explicit review/comment publish through `gh` / GitHub REST semantics
- remote IDs
- retry/error semantics
- only introduce a dedicated SDK if `gh` becomes a demonstrated limitation

### PR-R10 — Markdown review adapter

Only after code review repeat use is proven.

Anything after PR-R6 requires product evidence, not roadmap momentum.

---

## 19A. Attention-compression roadmap

This is the primary product-prioritization framework for LemonCrow Review.

> **Human attention is the currency. LemonCrow should reduce the amount of human attention required per trustworthy engineering decision.**

The long-term product metric remains:

> **correct changes understood and approved per human review hour**

Every proposed feature must answer one question before it enters the roadmap:

> **Which human-attention tax does this remove?**

If a feature adds information without making the reviewer read less, navigate less, remember less, reconstruct less, investigate less, coordinate less, or decide faster with equal-or-better confidence, it probably does not belong in the core product.

### 19A.1 The eight attention taxes

| Attention tax | Reviewer question | LemonCrow response |
| --- | --- | --- |
| **Triage** | What deserves my attention at all? | attention ordering, changed-since-review, mechanical/generated collapse, personal rules |
| **Orientation** | What is this change trying to accomplish? | Review Chapters, dependency order, commit/intent grouping |
| **Comprehension** | Why did this code change? | author/agent rationale, deterministic semantic annotations, compact AI hints, Ask This Change |
| **Navigation** | Where do I need to go to understand this? | clickable callers/impact/tests/types/history, related-source views, zero IDE scavenger hunts |
| **Verification** | How do I know this actually works? | tests/checks, provenance, screenshots, traces, preview/evidence brought into review |
| **Memory** | What have I already understood? | durable marks, changed-since-my-review, comment relocation, evidence staleness, revision reconciliation |
| **Coordination** | How do I turn judgment into action? | structured feedback, send to agent/peer, addressed-comment lifecycle, automatic new-revision handoff |
| **Scheduling** | Which review needs me now? | personal Review Inbox, your-turn state, agent/peer unified attention queue |

A useful design rule follows:

> **Once a human has paid the attention cost to understand something, LemonCrow should not charge them for it again unless the underlying evidence changed.**

### 19A.2 Unified annotation model

Annotations are one of the highest-leverage primitives because they can remove comprehension, navigation, verification, and coordination tax at the exact line/hunk/file where the reviewer is thinking.

But the source of an annotation must always be visible. Do not blend fundamentally different claims.

```text
[AUTHOR · Claude]
Why this code was written.

[LEMONCROW]
Deterministic evidence: signature changed · 7 callers · 3 outside patch.

[AI REVIEW · correctness]
Possible stale-cache path. Hypothesis, not fact.

[HUMAN · Pankaj]
Why can't context remain optional here?
```

Conceptual protocol:

```text
ReviewAnnotation
  source          author | lemoncrow | ai_review | human
  source_id       session / skill / reviewer
  revision_id
  path / range / symbol / file
  title
  body
  evidence[]
  confidence?     only when meaningful
  state
```

Rules:

- **author annotations explain intent**, not correctness;
- **LemonCrow annotations report observed/deterministic evidence**, not speculation;
- **AI review annotations are hypotheses** and should be compact by default;
- **human annotations carry actual review judgment**;
- progressive disclosure: one-line hint first, detail/evidence on demand;
- AI earns space only when it causes the human to read or investigate less.

Bad default:

```text
AI review: 37 long comments
```

Good default:

```text
● Possible stale-cache behavior
```

with explanation and evidence behind a click.

### 19A.3 Review Chapters: make large changes fit in a human head

A pull request, branch, or agent worktree is often too large a cognitive unit. GitHub's stacked-PR direction validates the need for smaller conceptual review units, but LemonCrow should not require authors to rewrite Git history before a change becomes understandable.

Create **virtual Review Chapters** inside any review:

```text
REVIEW — 165 files

01  Review persistence
    14 files
    ✓ reviewed

02  Review API
    17 files
    ● 2 need attention

03  Browser workspace
    11 files
    ○ unreviewed

04  Provenance
    9 files

05  Usage / models
    23 files

06  Tests
    64 files

07  Docs / install
    27 files
```

The same change should support several lenses:

```text
INTENT       what conceptual changes are being made?
DEPENDENCY   what should I understand first?
ATTENTION    what is risky / needs judgment?
COMMITS      how did the author actually build it?
```

Build the first version deterministically from:

- commits and conventional scopes/messages;
- directories/files;
- symbol and dependency relationships;
- LemonCrow's code graph;
- changed contracts and shared call sites.

AI may later improve chapter names or resolve ambiguous clusters, but it must not be required for trustworthy grouping.

When the source is a real stacked-PR series, map the real layers into the same chapter abstraction.

### 19A.4 Prioritized feature list

#### P0 — complete the attention-saving review loop

These directly remove daily reviewer work and should come before more analysis categories.

1. **Unified annotation protocol** — one rendering/state model for author rationale, LemonCrow evidence, AI hints, and human comments; visually distinct sources.
2. **Author/agent rationale annotations** — capture why the authoring agent made a change and place that explanation beside the relevant hunk/symbol; never make the human rediscover intent the author already knew.
3. **Review Chapters** — split a large change into conceptual bite-sized review units, with Intent / Dependency / Attention / Commit lenses.
4. **Automatic new-revision detection** — detect worktree/branch changes automatically, but never mutate the review under the reader; show `New revision available` and let the human advance the frontier.
5. **Feedback → agent → revision loop** — send structured feedback to one exact known agent session, receive the revision, and surface only what changed since review.
6. **Addressed-comment lifecycle** — `open → sent → author claims addressed → needs re-review → resolved by human`; the author/agent may claim it addressed a request but may not resolve the human's judgment.
7. **Incremental revision analysis** — reuse unchanged analysis so agent iteration becomes near-instant instead of rebuilding a 165-file review from scratch.
8. **Zero-navigation context** — make every surfaced caller, affected site, test, type, and relevant history entry directly inspectable without leaving the review.

**P0 gate:** a reviewer can supervise one real agent task end-to-end without manually copying prompts, reconstructing intent, re-reading unchanged work, or leaving LemonCrow to chase context.

#### P1 — compress the remaining judgment workload

9. **Review Skills** — optional correctness/security/API/accessibility/project-convention reviewers that emit the same compact AI-annotation protocol. They point; they do not own the verdict.
10. **Ask This Change / Ask This Hunk** — model assistance only on demand for questions such as `why is this necessary?`, `what assumption changed?`, `show me the relevant callers`, `summarize only the semantic change`.
11. **AI/mechanical change compression** — recognize large mechanical rewrites/renames/generated changes and summarize the repeated transformation while preserving a way to inspect every underlying diff.
12. **GitHub PR read adapter** — `lcr 25` / `lc review pr 25 --open`; import PR intent, commits, checks, existing comments and stacked-PR structure using `gh`, while LemonCrow remains the review surface.
13. **GitHub publish adapter** — explicitly publish selected human comments/review state only after peer-review use is proven; GitHub remains repository/merge infrastructure.
14. **Visual Review Evidence** — screenshots first, Playwright traces next, video only when time/motion is the subject; evidence is revision-bound and automatically becomes stale when code changes.
15. **Evidence↔code linkage** — connect screenshots/traces/checks to the changed units and show agent-inspection gaps, without asserting causality that was not observed.
16. **Compact verification view** — collapse many check/test failures into root-cause groups where evidence supports it; never turn silence into PASS.

**P1 gate:** reviewers demonstrably investigate less, run the branch manually less often, and choose LemonCrow over GitHub/IDE for at least some peer and agent reviews.

#### P2 — make the entire reviewing workday attention-aware

17. **Personal Review Inbox** — one queue for agent and human work, organized by `your attention`, `waiting on author/agent`, `new revision`, `comments addressed`, not by repository object type.
18. **Attention handoff / your-turn state** — Gerrit-style explicit ownership of whose judgment/action is currently required.
19. **Cross-review prioritization** — rank *reviews*, not just files, by what currently requires human judgment; avoid opaque ML scores and always explain the reason.
20. **Reviewer preferences/rules** — `not useful`, `always show this`, `usually collapse this path`; collect explicit history first and later distill repeated preferences into transparent local/team rules.
21. **Markdown/RFC review** — reuse the same annotation/frontier/feedback model only after code review is sticky, proving the abstraction really is `review work`, not `PR UI`.
22. **Shared/team review state** — synchronization, shared inboxes, permissions and organization controls only when multiple users are already asking to collaborate in LemonCrow.

**P2 gate:** LemonCrow becomes where a developer decides what deserves their next block of attention, not merely where they inspect one diff.

### 19A.5 Recommended implementation sequence

The near-term sequence should therefore be:

```text
R11  Unified annotation protocol
R12  Author/agent rationale annotations
R13  Review Chapters
R14  New-revision detection
R15  Claude feedback delivery (one exact host/session only)
R16  Feedback → revision → changed-since-review loop
R17  Addressed-comment lifecycle
R18  Incremental revision analysis

VALIDATE THE CORE LOOP

R19  Review Skills
R20  Ask This Change / Ask This Hunk
R21  GitHub PR + stacked-review read adapter
R22  GitHub comment publishing
R23  Visual evidence experiment
R24  Personal Review Inbox
```

Do not interpret the numbers as promises. After R18, stop and validate before continuing.

The active post-R18 validation checklist is [`2026-09-09-review-dogfood-completion.md`](2026-09-09-review-dogfood-completion.md). R19–R24 remain frozen until that checklist passes against the real `origin/main...feat/review-usage-model` review.

### 19A.6 Attention-saving feature test

Before implementing any new feature, answer all five:

1. **Which attention tax does it remove?**
2. **What reviewer action/context switch/re-reading does it eliminate?**
3. **Can we measure whether humans actually do less work because of it?**
4. **Could the feature itself create more material to review?**
5. **Can mature open-source infrastructure provide the commodity part while LemonCrow owns only the attention semantics?**

A feature should usually be rejected if question 1 or 2 has no concrete answer.

For AI specifically:

> **AI earns its place only when it causes the human to read less, navigate less, remember less, reconstruct less, or investigate less.**

### 19A.7 Metrics: measure human cost, not only machine cost

The old machine-efficiency metrics remain useful supporting diagnostics:

```text
model cost
agent turns
tokens
latency
```

But the review product should optimize and eventually measure:

```text
human review minutes
files / chapters actually requiring attention
unchanged reviewed work safely skipped
context switches avoided
changed-since-review items surfaced
comments that required re-review
manual branch checkouts avoided
abandonment to GitHub / IDE
false-positive attention findings
repeat review usage
```

Do **not** manufacture a `minutes saved` number until there is enough observed data to support it. Human-time claims need the same provenance discipline as cost claims.

The conceptual resource report is nevertheless useful:

```text
AGENT RESOURCES              HUMAN REVIEW RESOURCES
$23.10                       42 min observed review time
182M tokens                  7 files required attention
14 turns                     31 files collapsed / preserved
```

The strategic shift is:

```text
old LemonCrow: make agents cheaper and more context-efficient
new LemonCrow: make human engineering judgment scale with agent output
```

### 19A.8 Core product boundary

Do not turn attention compression into autonomous judgment.

LemonCrow may:

- eliminate obviously irrelevant review work;
- prioritize likely-important work;
- chunk a change into comprehensible units;
- explain author intent;
- surface deterministic code impact;
- provide optional AI hypotheses;
- bring verification evidence to the reviewer;
- remember prior human understanding;
- route human feedback back to the author;
- schedule the next item requiring judgment.

The human still decides whether the engineering is acceptable.

> **Compress the amount of human attention required to understand each change, without outsourcing the engineering judgment.**

---

## 20. Validation plan

Do not validate this by asking whether the UI looks nice.
---

## 19B. Intuitive journey, UX, and visual-system requirements

Attention compression is not only an analysis problem. The interface itself can create attention tax through setup friction, unclear state, excessive controls, visual noise, hidden actions, inconsistent terminology, or surprising navigation.

> **A feature that saves reviewer attention in the backend but spends it again in the UI has failed.**

Usability, interaction design, responsiveness, and visual hierarchy are therefore part of the core product — not a polish phase after the roadmap.

### 19B.1 The ideal journey

The default workflow should be understandable without documentation:

```text
1. OPEN
   lcr
       ↓
2. ORIENT
   "What changed? What matters? Where should I start?"
       ↓
3. REVIEW
   one coherent chapter/file at a time
       ↓
4. UNDERSTAND
   rationale + impact + evidence appear where needed
       ↓
5. JUDGE
   reviewed / request change / comment
       ↓
6. ACT
   send feedback to the known author/agent
       ↓
7. WAIT
   LemonCrow watches; reviewer does something else
       ↓
8. RETURN
   "3 things changed since you reviewed"
       ↓
9. FINISH
   human closes the review
```

At every stage the UI should answer three questions immediately:

```text
Where am I?
What needs me now?
What is the obvious next action?
```

If the reviewer has to remember a command, inspect documentation, interpret a mysterious score, or guess whether a button mutates state, the interaction needs simplification.

### 19B.2 First-run experience

The first useful review should require almost no setup.

Preferred path:

```text
cd repo
lcr
```

Then LemonCrow should:

- detect the repository and most natural change source;
- open the current local change / latest agent work automatically where unambiguous;
- explain what it selected in one short sentence;
- avoid configuration wizards before value is visible;
- degrade gracefully when provenance/index/CI is missing;
- offer optional enrichment only after the review is usable.

Bad first run:

```text
configure host
choose provider
configure review mode
select indexing strategy
choose repository source
configure browser
```

Good first run:

```text
Reviewing 14 changed files in your working tree.
3 need attention. Start with auth/session.py →
```

### 19B.3 One obvious primary action

Each screen/state should have one dominant next action.

Examples:

```text
new review             → Start review
file understood        → Reviewed + next
request written        → Send feedback
agent changed code     → Review changes
addressed comment      → Re-review
review complete        → Finish review
```

Secondary actions remain available but visually subordinate.

Do not create toolbars where every action has equal weight.

### 19B.4 Progressive disclosure

The default surface should show the minimum necessary to make the current decision.

Use three levels:

```text
LEVEL 1 — SIGNAL
● Signature changed · 7 callers

LEVEL 2 — EXPLANATION
Why this matters, affected files, agent inspection state

LEVEL 3 — EVIDENCE
source lines, graph edges, test records, provenance records
```

Do not put Level 3 on screen by default merely because LemonCrow can compute it.

The same rule applies to AI:

```text
● Possible stale-cache path
```

before any generated explanation.

### 19B.5 Stable spatial model

The workspace should become muscle memory.

Default desktop structure:

```text
┌────────────────────────────────────────────────────────────────┐
│ REVIEW / CHAPTER / PROGRESS / PRIMARY ACTION                   │
├──────────────────┬──────────────────────────┬──────────────────┤
│ ATTENTION        │ CHANGE                   │ CONTEXT          │
│ chapters/files   │ diff / outcome / source  │ why / evidence   │
│                  │                          │                  │
├──────────────────┴──────────────────────────┴──────────────────┤
│ status + keyboard hints                                       │
└────────────────────────────────────────────────────────────────┘
```

Do not frequently move controls or repurpose panes based on hidden state. When the center changes from diff → related source → outcome evidence, the surrounding navigation/context should remain stable.

### 19B.6 Visual hierarchy: attention should be visible, not loud

Use visual emphasis as an attention budget.

High emphasis only for:

- something requiring human judgment;
- changed-since-review;
- unresolved requested changes;
- stale/unknown evidence that changes what can be concluded;
- explicit errors.

De-emphasize:

- already reviewed unchanged work;
- mechanical/generated work;
- metadata;
- resolved comments;
- informational provenance that does not require action.

Avoid a dashboard of colored badges. If everything is highlighted, nothing is prioritized.

Recommended visual semantics:

```text
neutral      normal information
muted        safely de-prioritized / already handled
blue         new/change/navigation
amber        attention / uncertainty / needs judgment
red          failed/refused/objective error, used sparingly
green        observed pass / human-completed state, never AI "safe"
```

Color must never be the only carrier of meaning.

### 19B.7 Source identity must be visually obvious

Annotations should be recognizable before reading them:

```text
AUTHOR       intent / rationale
LEMONCROW    deterministic evidence
AI REVIEW    hypothesis
HUMAN        judgment / request
```

Use stable iconography, labels, typography, and border treatment. Do not make the reviewer inspect metadata to learn whether a sentence came from a model, the author, or deterministic analysis.

### 19B.8 Mouse and keyboard should describe the same workflow

Power users should be fast without making new users learn shortcuts.

Every important keyboard action must have a visible clickable equivalent, and vice versa.

Core vocabulary should stay small:

```text
j / k     next / previous visible item
] / [     next / previous attention item
r         reviewed + next
u         reopen
c         comment
/         search
s         split / unified
?         context
Esc       close transient detail / cancel
```

Do not keep adding one-letter shortcuts indefinitely. New functionality should usually live behind search/command palette/contextual actions rather than expanding the memorization surface.

### 19B.9 Preserve place and state

The UI should never make the human relocate themselves unnecessarily.

Preserve across reload/revision where valid:

- selected chapter;
- selected file;
- scroll position when content is unchanged;
- split/unified preference;
- collapsed groups;
- search/filter when useful;
- context pane state;
- reviewed state and comments, of course.

When content changes and a previous position is no longer meaningful, explain where the reviewer was moved and why rather than silently jumping.

### 19B.10 Never interrupt reading

Agent updates, new CI results, or evidence changes should not mutate the review surface underneath the human.

Prefer:

```text
New revision available · 4 files changed
[Review changes]
```

not an automatic diff refresh.

Notifications should accumulate quietly unless immediate action is genuinely required.

### 19B.11 Empty, loading, degraded, and error states are part of the journey

Every non-happy path should tell the reviewer:

```text
what happened
what LemonCrow still knows
what the reviewer can do next
```

Examples:

```text
No agent provenance recorded.
You can still review the code and impact normally.
```

```text
Full-suite result unknown.
Focused tests passed; no full-suite execution was recorded.
```

```text
This impact target cannot be read at the reviewed revision.
Open it in your editor →
```

Do not expose raw internal failure vocabulary when a human explanation exists.

### 19B.12 Speed is UX

The interaction budget should be explicit.

Targets for an already-created review session:

```text
navigation / mark / open context       perceptually instant
local UI state                         <100 ms target
cached source/diff transition          <200 ms target
new-revision detection                 a few seconds, non-blocking
incremental reconciliation             fast enough to keep flow
full cold analysis                     may take longer, with useful progress
```

Do not block navigation on analyses that can load progressively.

The reviewer should be able to start reading before every enrichment finishes.

### 19B.13 Responsive information density

The product is for engineers reviewing substantial code, so density is useful; clutter is not.

Design for:

- large desktop monitors first;
- laptop widths without requiring constant horizontal resizing;
- collapsible context/attention panes;
- readable code line length;
- clear focus states;
- predictable font sizing and spacing;
- no gratuitous card-heavy SaaS dashboard aesthetic around source code.

The diff/code should remain the visual center of gravity.

### 19B.14 Accessibility and ergonomics

At minimum:

- full keyboard navigation;
- visible focus states;
- semantic buttons/labels;
- sufficient contrast;
- color-independent state indicators;
- reduced-motion preference respected;
- tooltips are supplemental, never required for core operation;
- no hover-only actions for required workflow steps.

### 19B.15 Journey-level acceptance tests

Do not only test components. Test complete reviewer journeys.

Required scenarios:

1. **First local review:** new user runs `lcr`, understands what to review, reviews several files, finishes without docs.
2. **Agent revision:** reviewer comments, sends feedback, leaves, returns to a clear `changed since my review` queue.
3. **Large change:** reviewer uses chapters + attention to navigate a 100+ file change without scanning a flat list.
4. **Degraded analysis:** no index/provenance/CI is available; the review remains usable and clearly honest.
5. **Peer PR:** reviewer opens a GitHub PR and uses the same interaction vocabulary learned from local agent review.
6. **Visual change:** reviewer moves from code to screenshot/trace evidence and back without losing place.
7. **Reload/restart:** review state and location survive where safe.

For usability validation, hand the tool to a new engineer and explain nothing. Observe:

- where they hesitate;
- what they click expecting a different result;
- when they ask what a term means;
- when they leave for GitHub/IDE/terminal;
- what they fail to notice;
- how often they need to remember state themselves.

### 19B.16 UX feature gate

Every implementation PR touching the review journey should answer:

1. Is the next action more obvious or less obvious?
2. Did this add a concept/term/control the reviewer must learn?
3. Does this reduce or increase visual competition for attention?
4. Does the reviewer preserve their place and prior understanding?
5. Can a mouse-first and keyboard-first user complete the same task?
6. What happens in loading/degraded/error states?
7. Is there a simpler interaction that achieves the same attention saving?

> **The best LemonCrow interaction is often the one the reviewer barely notices.**

---

## 20. Validation plan
Give engineers real agent-generated changes and observe review behavior.

### 20.1 First design partners

Target 5–10 engineers who already use coding agents heavily.

Ask them to use LemonCrow on:

1. their own agent-generated local change;
2. a peer's PR;
3. later, an agent plan/RFC.

### 20.2 Questions after a review

- What did you understand faster than in your normal tool?
- Which LemonCrow signal made you inspect something you otherwise would have missed?
- Which signal was noise or wrong?
- Did you trust the “changed since review” state?
- Did sending feedback back to the agent save any translation/copy-paste work?
- Would you choose this over your normal review surface next time?
- What made you leave LemonCrow and open GitHub/IDE anyway?

### 20.3 Behavioral gates

Do not use vanity signups as the main evidence.

Progress when:

```text
several users voluntarily use it for multiple reviews
review sessions are reopened after author revisions
review marks/comments materially reduce re-reading
users trust impact findings enough to navigate from them
at least some users want the same surface for both agent and peer changes
```

A particularly strong signal is:

> “I no longer want to review this in GitHub.”

### 20.4 North-star metric

Long-term:

> **correct changes understood and approved per human review hour**

Near-term proxies:

- repeat reviews per active reviewer;
- time from review open to completion;
- percentage of unchanged reviewed units preserved across revisions;
- number of changed-since-review units surfaced;
- annotations delivered back to authors/agents;
- false-positive / unhelpful attention signals;
- abandonment to GitHub/IDE during a review.

Never optimize raw “reviews completed faster” without checking review quality.

---

## 21. What to explicitly not build

Until real users pull these forward, freeze:

- another autonomous AI reviewer;
- multi-agent review swarms;
- cloud execution/sandboxes;
- worker pools;
- Git hosting;
- merge queue;
- CI replacement;
- general project management;
- broad Slack/Jira/Confluence ingestion;
- screenshot capture harness, preview/container orchestration, visual-diff algorithm, Playwright trace viewer, video player (§25);
- custom source-code editor;
- team admin/governance polish;
- review analytics dashboard before repeated individual use;
- every-host live steering;
- proprietary closed review engine solely for monetization.

Reuse existing LemonCrow capabilities when they help review; otherwise leave them alone.

---

## 22. Competitive boundary

Do not try to win on “prettiest diff renderer.” Hunk and others can do that well.

Do not try to win on “most AI review comments.” CodeRabbit/Greptile/Qodo/Copilot can compete there.

Do not try to replace GitHub's repository/merge machinery.

LemonCrow's defensible center should be:

```text
             CODE INTELLIGENCE
 symbols · callers · contracts · history · impact
                      |
                      v
AGENT PROVENANCE -> REVIEW SESSION <- HUMAN REVIEW STATE
 reads · edits        |              marks · comments
 tests · model        |              review frontier
                      v
             REVISION-AWARE FEEDBACK
               human or agent
```

In words:

> **LemonCrow knows enough about both the codebase and the authoring agent to tell a human what changed, what it affects, what they already reviewed, and what changed after their feedback.**

That is more differentiated than another diff viewer or another AI reviewer.

---

## 23. Landing-page direction

The landing page should now lead with the workflow shift, not context optimization or cost.

Possible top-level narrative:

> **Agents write more code. You still have to understand it.**

Supporting explanation:

> LemonCrow helps you review what humans and coding agents changed, what the change affects, what was actually tested, and what changed since your last review.

A more conversational problem statement remains useful in demos/interviews:

> **I have agents writing a lot more code. Help me understand what they did.**

Show the product before benchmark numbers:

```text
1. review order
2. semantic impact outside the diff
3. agent provenance / test evidence
4. changed since my review
5. feedback sent back to the authoring agent
```

Then supporting proof:

```text
local-first
open source
works across coding agents/models
usage visibility
self-hosted models
context/code intelligence underneath
```

Cost savings becomes secondary proof, not the reason the product exists.

---

## 24. Final product boundary

The surrounding ecosystem can keep owning its specialties:

```text
Claude/Codex/OpenCode/etc.  -> generate code
Git/GitHub                  -> store/share code
CI                          -> execute checks
humans                      -> make engineering judgment

LemonCrow                   -> make the work reviewable
```

The first repeatable behavior to earn is:

> **An engineer receives a substantial agent-generated change, opens LemonCrow, understands the important parts, leaves precise feedback, lets the agent revise it, and returns to see only what now needs re-review.**

Then prove the same interaction works for a peer's PR.

Only after that should LemonCrow expand toward a general review inbox or team platform.

---

## 25. Outcome evidence — the third review dimension

Research addendum, 2026-09-08. Supersedes the sketch in §18 Phase 2.5 and §19 PR-R5V.

Screenshots, traces and previews are **evidence of the outcome**, not decorative PR attachments. The reviewer is answering two different questions and today only the first is served:

```text
what was implemented          -> CODE
what the implementation does  -> OUTCOME
```

So the workspace grows a third dimension beside the two it already has:

```text
CODE            OUTCOME            EVIDENCE
```

```text
OUTCOME

Desktop
  [ screenshot ]
  ✓ reviewed

Mobile
  [ screenshot ]
  ● changed since my review

Interaction
  checkout-flow.trace
  ● unreviewed

Live
  Open preview ↗
```

The code diff stays alongside it. The reviewer stops checking out the branch and reconstructing the environment to answer “does it actually work?”

### 25.1 The load-bearing rule: evidence belongs to a revision

This, not the gallery, is the feature.

```text
rev 4
  ✓ checkout-mobile.png   reviewed

(agent changes checkout code again)

rev 5
  checkout-mobile.png
  ⚠ STALE — captured for rev 4
```

LemonCrow must never keep presenting a rev-4 screenshot as evidence that rev-5 code works. That is the same failure class as a stale `reviewed` mark on changed code, and it is worse than showing no evidence at all — it is a false proof.

This is §6 applied to behavior instead of source:

```text
CODE
  2 units changed since review

VISUALS
  1 screenshot changed
  3 unchanged

INTERACTIONS
  checkout trace stale
```

Agentic iteration is exactly what makes this necessary: agents re-run the change faster than a human re-checks the proof.

Evidence therefore reuses the PR-R5 machinery rather than inventing a parallel one — the same revision binding, the same state machine, the same frontier.

### 25.2 `ReviewEvidence`

One abstraction covers agent output, human capture, test output and CI artifacts:

```text
ReviewEvidence

kind
  screenshot
  visual_diff
  playwright_trace
  video
  live_preview
  image
  document

source
  agent | human | test | ci | external

revision
content_hash
review_state        reviewed | unreviewed | changed-since-review | stale
```

Both workflows land on the same surface — which is the point of one review tool for human and agent work:

```text
Agent finished checkout change      Sarah opened PR #422
  ✓ desktop screenshot               ✓ Vercel preview
  ✓ mobile screenshot                ✓ Storybook screenshot diff
  ✓ checkout Playwright trace        ✓ Playwright trace
  ✓ focused tests
```

### 25.3 Screenshots are the default

Rank them well above video. For most visual changes

```text
BASELINE        NEW        DIFF
```

is the fastest possible review, and it is what Chromatic and Argos optimize for: show only the visual states that changed, approve those.

LemonCrow's differentiator is binding that back to code intelligence — neither code review nor screenshot review alone can say this:

```text
Checkout mobile screenshot changed

Related code:
  CheckoutForm.tsx
  PaymentSheet.tsx

Semantic changes:
  PaymentSheet layout contract changed
  responsive breakpoint changed

Visual:
  [before] [after] [pixel diff]

Agent:
  inspected CheckoutForm.tsx
  did not inspect MobileLayout.tsx ⚠
```

The agent-inspection gap comes from provenance we already record at edit time (§Phase 0). Where a binding is a guess, say so; never assert a cause we did not observe.

### 25.4 Interactions: Playwright traces before video

A video says *something happened*. A Playwright trace says what:

```text
click Checkout -> DOM before/after -> screenshot -> network -> console -> timing -> next action
```

Traces already carry filmstrip, DOM snapshots, network, console and timings; Playwright itself recommends them over plain video for exactly this reason.

```text
INTERACTION EVIDENCE

Checkout happy path
✓ Add product
✓ Open cart
✓ Checkout
✓ Payment accepted

[ ▶ interaction filmstrip ]

12 actions · 4.2 sec

Console       0 errors
Network       all 2xx
Trace         Open full trace ↗
```

**We do not build a trace viewer.** We ingest `trace.zip` and open Playwright's.

### 25.5 Video only where time and motion are the content

Animations, transitions, drag/drop, gestures, scrolling, perceived performance, streaming, media, complicated mobile interactions.

```text
Sidebar animation   00:00–00:04
[ ▶ 4 sec clip ]

Comment at 00:02.3
"the panel jumps here instead of easing"
```

Timestamp comments are a later nicety. **Do not make every agent record a video** — that is review noise, and noise is the thing this product exists to remove.

### 25.6 Comment on the result, not only the cause

A reviewer should not have to locate the responsible CSS line before being allowed to give feedback:

```text
┌─────────────────────────┐
│       Checkout              │
│                 ●           │
└─────────────────────────┘

"This button is clipped on mobile."
```

The exported feedback bundle (§PR-R6) carries the region, the revision and the code binding:

```text
Visual review feedback

checkout-mobile.png
region x=.71–.91, y=.64–.73

"The button is clipped on mobile."

Evidence revision: rev-6
Related changed files:
  Checkout.tsx
  checkout.css
```

Regions are normalized fractions, so they survive a differently sized capture. A region annotation follows the same anchoring honesty rule as a code annotation: it binds to an evidence revision, and when that evidence is replaced it is re-anchored only if unambiguous — otherwise explicitly orphaned.

### 25.7 Own review, not environments

ShipIt runs an entire app stack per agent session in isolated containers with live previews. Correct for ShipIt; wrong for us now. It leads straight to Docker, sandboxes, networking, ports, databases, preview hosting, tunnels, process management and credentials — precisely the platform expansion §21 exists to prevent.

LemonCrow **consumes** what already exists — `localhost:3000`, a Vercel preview, a running dev server, Storybook, Playwright artifacts, CI screenshots, a mobile emulator URL — and makes it reviewable.

> Own review. Don't own environments.

### 25.8 OSS boundary for this slice

The standing rule extends unchanged:

> **OSS captures and renders the evidence. LemonCrow decides what the evidence means for the review.**

Do not build: a capture harness, a headless-browser driver, preview or container orchestration, a visual-diff/pixel-comparison algorithm, a trace viewer, a video player, an image storage service.

Build only: the evidence record, its revision binding and staleness, its review state, its binding to changed units and to agent inspection, and the panes that present them.

### 25.9 Gates

1. A rev-4 screenshot is never presented as proof for rev-5 code — construct the case and prove it fails closed.
2. Evidence review state answers “what changed since I reviewed?” for behavior as reliably as §6 does for code, with false-survival counted as the top defect.
3. A UI-heavy design partner reviews a visual change without checking out the branch.
4. Nothing in the evidence pane asserts a code↔outcome binding we did not observe.
