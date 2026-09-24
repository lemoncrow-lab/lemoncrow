# Developer review improvements — must have and nice to have

Status: Proposed product scope. This document does not implement product changes.

Date: 2026-09-17

Code baseline: `0a169716e` in `lemoncrow-dev`.

Research input: Fabian Roth / Pankaj Kumar interview, September 17, [recording](https://fathom.video/share/yz-oL65PvyWw4rutqNbPNcs1dE51ohNs).

## 1. Product decision

Make the existing browser review workflow useful from beginning to end:

> Understand the change → request a correction → get the correction back → verify the result without rereading unchanged work.

Prioritize functionality that makes this loop work. Customer interviews reveal problems and possible explanations; they are not implementation checklists. A request from one person does not automatically become a requirement.

**VS Code integration is a nice to have and is outside current scope. There is no extension work, feasibility spike, or editor-transport refactor in this plan.**

The codebase already has most of the foundation: a continuous diff reader, impact ranking, durable review state, incremental refresh, feedback export, direct delivery to an exact inactive Claude session, and revision-bound evidence. Improve demonstrated gaps in these capabilities instead of building them again.

### How a change earns priority

A must-have change should meet all of these conditions:

1. It supports understanding, correction, or verification in the core review loop.
2. It addresses a reproducible gap, misleading state, or avoidable loss of work.
3. It benefits the intended developer audience beyond one person's preferred setup.
4. It can be delivered as a focused extension of the existing implementation.

Must-have outcomes are not all new features. When existing functionality already satisfies an outcome, verify and retain it. Do not manufacture additional work to match the interview.

This document narrows the earlier proposal. The [canonical Review Reader spec](2026-09-12-review-reader-spec.md) remains the product foundation. The completed [incremental review plan](2026-09-15-local-incremental-review-plan.md) remains the refresh contract. The [platform parity analysis](2026-09-15-code-review-platform-parity.md) continues to define the boundary with SCM platforms.

## 2. Must have — current scope

Ranked by contribution to the product's core job:

| Rank | Required outcome | Why it matters | Current state and minimum work |
| --- | --- | --- | --- |
| 1 | **Understand the important changes quickly.** Show the actual diff, useful review order, and concrete impact with links to evidence. | A review tool must help the developer decide what deserves attention. | Ranking, change stories, overview, and context already exist. Fix confusing or ungrounded output and make important context accessible; do not add another generic AI summary. |
| 2 | **Turn comments into actionable corrections.** Make it clear how feedback reaches the author/agent and what happened to it. | Commenting alone does not complete the correction workflow. | Export and exact-Claude delivery are wired. Improve discoverability, preview/send consistency, delivery status, and failure recovery. Keep copy/export as a valid fallback. |
| 3 | **Review the result without losing prior work.** Preserve valid judgments, reopen changed targets, and retain comments and draft text. | Repeated agent edits must not force a complete restart or leave changed code looking reviewed. | Content-bound judgments and automatic incremental refresh already exist. Preserve them; fix remaining gaps such as destructive draft refresh or responses that are not surfaced. |
| 4 | **Know what was actually verified.** Connect check results and existing previews/artifacts to the reviewed revision, and make unknown or stale evidence clear. | A confident-looking review with unrelated evidence is misleading. | Revision-bound evidence already exists. Ensure it is understandable in the review flow; do not build a new test framework or validation product. |

These four outcomes define the release scope. Each implementation task must name the particular gap it closes and the acceptance scenario that proves it.

### 2.1 Understand the change

The reviewer should be able to answer:

- What changed?
- Which part should I inspect first, and why?
- What outside the diff might be affected?
- Where is that claim supported, and what is unknown?

Use the existing diff, outline, overview, impact data, and context drawer. Favor short factual reasons linked to definitions, callers, or checks. Keep longer author rationale available on demand and label it as an author claim.

Show the code as soon as source preparation allows it; impact enrichment can finish later. When the index is absent, stale, or scoped, retain a usable diff and explain the limits. Unknown reach must not look like zero reach.

A compact default view is useful, but a text-length rule or a new layout is not the deliverable. The deliverable is successful comprehension.

Acceptance:

- On a representative change, the reviewer can identify the main change and an important affected dependency without reading a long generated essay.
- A promoted file or target has an inspectable reason.
- Missing analysis does not hide files, block source review, or produce a false low-risk claim.
- Existing source, API, or rendered previews remain accessible where they help verify the actual behavior.

### 2.2 Comment → agent → correction

Make the existing feedback action easy to find when unresolved human comments exist. The reviewer sees the included comments, destination, and available action.

Keep the current supported paths:

- Send directly to an eligible exact inactive Claude session.
- Copy/export feedback when direct delivery is unsupported, unavailable, or blocked.

Saving a comment does not run an agent. Copying does not mean delivered. Sending does not mean fixed. An author claiming “addressed” does not mean the reviewer accepted the correction.

The concrete backend gap is that the current export and send endpoints independently rebuild a bundle from the latest state. The prepared preview therefore does not contractually identify the exact content later sent. Bind send to the previewed revision and feedback content; if either changes, require an updated preview.

A second gap is that delivery records are written after dispatch returns. Make retries and concurrent sends safe before improving the prominence of the direct-send action.

Minimum implementation direction:

- Extend the current route with expected revision, feedback-content identity, and a request/operation identity.
- Validate the preview's identity against authoritative state before dispatch.
- Record the operation before starting the external process so repeated requests can return its known state.
- If dispatch may have happened but acknowledgment is missing, report an uncertain outcome and inspect it before retrying.
- Reuse existing review and delivery storage. Add only the fields/state needed for these guarantees.

A generalized batch service, delivery queue, automatic retry system, multi-agent routing layer, and new 24-hour draft-expiry policy are not requirements.

Acceptance:

- A reviewer can leave a comment and find the next handoff action without coaching.
- A code/comment change after preview cannot silently alter the sent request.
- Repeated submission of the same operation does not launch a second agent.
- Missing CLI, an active session, or uncertain provenance produces an understandable outcome and a usable copy/export fallback.
- The delivered request stays associated with the correct repository, review, and author session.
- A supported live Claude version can complete a disposable-repository smoke scenario; mocked delivery tests alone do not establish compatibility.

### 2.3 Re-review without lost work

Preserve the current settled-source refresh and target reconciliation. Unchanged reviewed targets remain reviewed; changed targets return to attention.

The current reader already defers automatic refresh while a draft is open. Manual refresh can discard a draft when its source changes. Retain its text for recovery and require the human to choose a new anchor rather than silently moving or losing it. This does not require cross-device or persistent draft synchronization.

Surface author responses independently of source-byte changes. A reply saying why no edit was made is still a response. It should not require manufacturing a new source revision to become visible.

An addressed claim belongs to the request version the author received. Verify that editing a request makes an older claim visibly out of date; extend the existing annotation history if needed.

Acceptance:

- Editing one reviewed target reopens that target while preserving unrelated reviewed work.
- A stale network response cannot replace current revision state.
- A reviewer can recover draft text after an update changes its anchor.
- Ambiguous comment relocation is shown as orphaned with its original context.
- A response with no source changes is visible after refreshing review metadata.
- Only the human resolves their comment or records acceptance.

### 2.4 Trustworthy verification

Keep existing check results, evidence, previews, and unit/integration tests. Show what ran, its observed result, and whether it applies to the selected revision.

An artifact is not automatically a passing check. An old successful run is not automatically proof for new code. Missing results remain missing.

Use ordinary comments to request additional validation, such as “check the expired-token behavior.” A new acceptance-criteria schema is unnecessary until the existing workflow proves insufficient.

Acceptance:

- The reviewer can distinguish passing, failing, not-run, unknown, and stale evidence.
- Evidence remains bound to its recorded revision.
- The finish view exposes unresolved comments and verification gaps.
- A screenshot, an author statement, or a successful send cannot mark a review approved.

## 3. Baseline constraints — preserve, do not expand into projects

These are implementation constraints on the must-have work, not separate product initiatives.

| Constraint | Required now | Not required now |
| --- | --- | --- |
| Human work is durable | Preserve comments, judgments, history, and recoverable draft text during the loop. | A new collaboration or draft-sync platform. |
| Local data handling is accurate | Keep current loopback/auth boundaries and state clearly which configured actions use the network. Correct misleading claims when updating affected documentation. | A privacy dashboard, managed offline mode, enterprise policy engine, or broad compliance program. |
| Review stays responsive | Use existing source-first preparation, lazy loading, and incremental refresh. Measure ordinary review tasks and fix observed blocking work. | A new monorepo benchmark program or speculative performance rewrites. |
| Existing savings remain useful | Keep the indexing/runtime benefits and avoid regressions from review changes. | New cost dashboards, pricing work, or a second savings roadmap. |
| Changes remain focused | Preserve current browser/CLI workflows and shared review state; keep added state within existing review retention. | A new transport abstraction just to prepare for a hypothetical extension. |

Remote aggregate telemetry is enabled by default in the current implementation and has explicit opt-out controls. Configured agents, model services, updates, and user programs can have their own network behavior. Accurate documentation is appropriate; a blanket promise that nothing can leave the machine is not.

No new telemetry collection or deployment mode is needed to deliver the current scope.

## 4. Nice to have — deferred, not scheduled

These ideas do not have a milestone or implementation commitment. Revisit an item only when repeated usage exposes a material limitation; completing the must-haves does not automatically activate this list.

| Idea | Why defer it | Evidence that could justify revisiting it |
| --- | --- | --- |
| **VS Code integration** | A preferred location for the UI does not add the core review capability. It adds packaging, transport, lifecycle, and support work. Explicitly outside current scope. | Multiple active users repeatedly abandon an otherwise useful browser loop because switching tools prevents use. |
| Direct delivery to more agent hosts | Copy/export already provides a usable path. Each direct integration introduces identity and continuation constraints. | Repeated friction from users of a specific host and a reliable continuation mechanism. |
| Advanced change narratives or richer grouping | Existing deterministic ranking, chapters, and context already provide a foundation. More AI text may add reading work. | Reviewers still cannot understand representative changes after focused improvements to the current view. |
| A dedicated validation-requirements model | Comments and revision-bound evidence can express the initial need. A new schema and approval workflow add complexity. | Repeated loss or ambiguity of human expectations that the existing workflow cannot handle. |
| Statistical/research validation features | The distribution-checking example is domain-specific and does not establish a general product requirement. | A validated customer segment with repeatable evaluation needs. |
| A privacy/status dashboard or managed offline profile | Existing local deployment and opt-out controls address the immediate setup. | Concrete repeated configuration failures or contractual deployment requirements. |
| Extensive scale benchmarking and new indexing controls | Large-repository concern was speculative, and current indexing measurements already exist. | Reproduced review latency, memory, coverage, or indexing problems on relevant repositories. |
| More preview formats or visual polish | Existing surfaces are sufficient to evaluate the core workflow. | A common review task cannot be understood or verified with the present surfaces. |
| A formal pilot analytics/event pipeline | Direct observation and local timing are sufficient for the first functional iteration. | Repeat adoption questions that cannot be answered without systematic measurement. |

## 5. Not pursuing from this feedback

The interview does not justify repository hosting, becoming “the new GitHub,” merge queues, a full IDE, a generic agent runner, or automatic AI approval.

Hosted deployment and team collaboration may have independent product justification, but this interview is not a reason to expand them. Existing work in those areas is unaffected.

Do not replace unit tests or code review based on a forecast that humans will eventually read less code. The immediate product should make current review and verification more useful.

## 6. Implementation map

“Present” means inspected source is wired or has existing test definitions; it does not mean live host behavior was exercised during this design task.

| Area | Existing code | Focus of any change |
| --- | --- | --- |
| Reader and handoff controls | [ReviewReader.tsx](../../frontend/src/review/ReviewReader.tsx), [ReaderHeader.tsx](../../frontend/src/review/ReaderHeader.tsx), [reviewApi.ts](../../frontend/src/review/reviewApi.ts) | Findability of the existing action, truthful state labels, response refresh, and draft recovery. |
| Change understanding | [ordering.py](../../src/lemoncrow/pro/capabilities/review/ordering.py), [chapters.py](../../src/lemoncrow/pro/capabilities/review/chapters.py), [preparation.py](../../src/lemoncrow/pro/capabilities/review/preparation.py) | Relevant explanations with evidence and visible missing analysis; preserve deterministic behavior. |
| Feedback correctness | [feedback.py](../../src/lemoncrow/pro/capabilities/review/feedback.py), [api.py](../../src/lemoncrow/pro/capabilities/review/api.py), [delivery.py](../../src/lemoncrow/pro/capabilities/review/delivery.py) | Preview/send consistency, operation identity, safe repeat requests, and explicit uncertain outcomes. |
| Review/response state | [session_models.py](../../src/lemoncrow/pro/capabilities/review/session_models.py), [store.py](../../src/lemoncrow/pro/capabilities/review/store.py), [anchors.py](../../src/lemoncrow/pro/capabilities/review/anchors.py), [mcp_server.py](../../src/lemoncrow/gateway/adapters/mcp_server.py) | Preserve existing ownership and history; connect claims to the relevant request version if missing. |
| Verification | [evidence_capture.py](../../src/lemoncrow/pro/capabilities/review/evidence_capture.py) and existing evidence projections | Make current/stale/missing status clear. Reuse the current model. |
| Existing boundaries | [workspace.py](../../src/lemoncrow/pro/capabilities/review/workspace.py), [telemetry/config.py](../../src/lemoncrow/core/service/telemetry/config.py), [privacy.md](../setup/privacy.md) | Preserve local security and accurate claims; no new deployment subsystem. |
| Existing scale work | [engine.py](../../src/lemoncrow/pro/capabilities/code_context/engine.py), [BENCHMARKS.md](../../BENCHMARKS.md) | Reuse current indexing and measured results; optimize only where a relevant review scenario demonstrates a problem. |

New persistent delivery fields must follow the review's existing retention/purge lifecycle. Additive changes must not reset human annotations or judgments. Do not commit to a new `FeedbackBatch` object before the narrower extension above proves insufficient.

## 7. Focused delivery sequence

### Step 1: Exercise the existing loop and identify actual failures

Use a representative local change and a disposable agent session. Record which must-have acceptance criteria already pass and which fail.

Include one important dependency outside the diff, a human correction, a second revision, and evidence from both revisions. Measure time to usable diff and time spent finding the handoff. Do not impose invented performance targets before seeing the baseline.

Output: a short, concrete list of gaps. A capability that already works gets no new implementation task.

### Step 2: Complete correction and re-review

Fix the demonstrated gaps in feedback handoff, preview/send consistency, delivery outcomes, response freshness, and preservation of human work.

Use the existing reader, API, store, and Claude adapter. Each change should be justified by a failure scenario, with focused tests at the owning boundary.

### Step 3: Improve understanding and verification where needed

Use the same scenarios to simplify unclear orientation and expose relevant evidence. Retain the current diff-first interface and progressive disclosure.

If existing ranking, evidence display, or refresh already passes, preserve it. Do not redesign the whole UI to satisfy a phrase from the interview.

### Stop condition

The browser loop meets the four must-have outcomes on representative tasks. Remaining issues are understood and accurately surfaced.

Stop this milestone there. VS Code and the deferred list are not next steps by default.

## 8. Verification and product learning

Reuse and extend the existing tests according to the actual implementation changes:

- [test_review_delivery.py](../../tests/gateway/test_review_delivery.py) and [test_review_api.py](../../tests/gateway/test_review_api.py): correct destination, stale preview, repeat requests, and uncertain outcomes.
- [test_review_feedback_loop.py](../../tests/gateway/test_review_feedback_loop.py): preserve the complete feedback → correction → re-review invariant and reject stale author claims where applicable.
- [ReviewReader.test.tsx](../../frontend/src/review/ReviewReader.test.tsx): existing incremental behavior, draft recovery, response updates, clear handoff, and copy failures.
- [test_review_attention_ux.py](../../tests/gateway/test_review_attention_ux.py) and [test_review_ordering.py](../../tests/gateway/test_review_ordering.py): inspectable reasons, deterministic ordering, and honest unknowns.
- [test_review_workspace_server.py](../../tests/gateway/test_review_workspace_server.py): retain current loopback/auth and repository boundaries if affected.

Critical invariants: no loss of human feedback, no wrong-session delivery, no duplicate agent launch from a repeated operation, no automatic acceptance of an author claim, and no changed code or stale evidence presented as already reviewed/current.

Dogfood the workflow, then observe a few additional developers if available. Look for successful completion, fewer avoidable rereads, correct identification of consequential issues, and voluntary repeat use. One enthusiastic interview, one failed install, or one preference should not determine the roadmap.

A formal five-person study, remote event collection, and a broad benchmark matrix are not release requirements. Gather additional evidence in proportion to the decision being made.

## 9. How the interview informs this scope

| Feedback | Product decision |
| --- | --- |
| Too much code to review; difficult to understand agent changes | Keep understanding and accurate incremental review as core outcomes. |
| Wants comments sent back to Claude | Finish and validate the existing correction loop. |
| Long AI explanations are ignored | Prefer concise evidence-linked context; do not create a summary product. |
| Lives in VS Code | Record as a preference; defer integration. |
| Cannot share company code | Preserve local boundaries and accurate data-handling claims; do not invent an enterprise project. |
| Expects human-directed validation to matter more | Keep current evidence trustworthy; defer a dedicated validation framework. |
| Runs out of agent budget | Retain the existing savings benefit. |
| Questions large-repository indexing | Use current measurements and investigate demonstrated limits. |
| Suggests becoming GitHub | No product commitment. |

The transcript has mixed speaker attribution in places. Clear customer statements support the problems above; they do not establish broad demand, willingness to pay, or promised usage.

## 10. Design-task record

This scope is grounded in the inspected code baseline and the user's decision to prioritize functionality. It supersedes the broader delivery commitments in the earlier version of this document, including the VS Code spike, mandatory offline-profile work, dedicated batch-service design, and formal pilot gates.

Only this design document was changed. Future acceptance scenarios listed here have not been executed as part of this documentation update.
