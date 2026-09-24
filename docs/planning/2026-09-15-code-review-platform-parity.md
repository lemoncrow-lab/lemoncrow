# LemonCrow Review — code-review platform parity and product decisions

**Status:** competitive research + product parity baseline
**Research date:** 2026-09-15
**Scope:** current `lc review` manual Review Reader, local incremental review, and the hosted-review direction
**Decision lens:** what LemonCrow should defend, add, integrate, or deliberately not build

---

## 0. Executive decision

LemonCrow should **not** try to reach parity by becoming another GitHub/GitLab/Graphite clone.

The market has three largely separate product layers:

1. **SCM / merge workflow** — GitHub, GitLab, Bitbucket, Azure Repos, Gerrit.
2. **human review workflow overlays** — Reviewable, Graphite, parts of Gerrit.
3. **AI reviewer agents** — GitHub Copilot code review, CodeRabbit, Qodo, Greptile, Bito, Graphite Agent.

`lc review` is strongest where those layers are weakest: preserving **human judgment over changing agent-produced work** at a finer granularity than a file, while keeping impact, provenance, verification, rendered behavior, and uncertainty in the same review surface.

The product should therefore follow this boundary:

> **LemonCrow owns review understanding, human judgment state, revision reconciliation, evidence, and agent feedback. Existing SCMs continue to own repositories, pull requests, branch protection, merge queues, and merge execution.**

The highest-value parity gaps are not merge mechanics. They are reviewer-loop capabilities that fit LemonCrow's existing model:

1. **Executable code suggestions** with apply / batch / undo semantics.
2. **Explicit turn ownership** for discussions: waiting on author vs waiting on reviewer.
3. **Arbitrary revision comparison + target history**, building on the immutable revisions already stored.
4. **Hosted review inbox / attention routing** once multi-user hosted review exists.
5. **Reviewer scopes / ownership / policy integration** in hosted mode, preferably importing CODEOWNERS and host policy rather than inventing another ownership format.
6. **Adapters for external AI-review findings** so CodeRabbit/Copilot/Qodo/Greptile/Bito findings can become source-labelled evidence/annotations without letting an AI reviewer become human review authority.

Do **not** spend product effort rebuilding merge queues, branch protection, stack rebasing, repository hosting, project management, generic AI review essays, autonomous AI approval, or a full IDE.

---

## 1. What research already existed in LemonCrow

There was already substantial review research, but it was aimed mostly at **architecture and implementation substrate**, not broad product parity.

Relevant existing documents:

- `docs/planning/2026-09-08-review-first-developer-workspace.md`
  - established human review as authority;
  - made durable review state more important than summaries;
  - established `unknown != zero`;
  - made the source model source-agnostic and delivery-aware;
  - proposed changed-since-review, semantic impact, provenance, verification, and agent feedback.
- `docs-internal/review-workspace-design.md`
  - evaluated Pierre diffs, Hunk, Plannotator, Reviewable and Pyor for architecture/implementation shape;
  - explicitly rejected importing another product's review-state model;
  - established the rule that third-party systems may render/transport review while LemonCrow owns review identity and reconciliation.
- `docs/planning/2026-09-12-review-reader-spec.md`
  - is the canonical current product contract;
  - moved from a three-pane dashboard to a continuous manual review reader;
  - introduced non-overlapping `ReviewTarget`s as the review-progress denominator;
  - made changed-since-review, uncertainty, and human authority first-class.
- `docs/planning/2026-09-12-review-reader-execution.md`
  - records implementation and dogfood through the current continuous reader, revision delta, comments, Finish, search, ordering, safe bulk completion, file-scoped verification, overview, accessibility and virtualization hardening.
- `docs/planning/2026-09-15-local-incremental-review-plan.md`
  - makes local working-tree review revision-driven and incrementally refreshed;
  - preserves unchanged loaded patches and human state while invalidating only changed source;
  - makes settled source changes hot-refresh through the same revision path as manual refresh.

### Correction to the older competitor claim

`docs-internal/review-workspace-design.md` says that no evaluated candidate had a changed-since-review frontier. That statement is too broad when interpreted as a market claim.

Reviewable explicitly tracks review marks per file and revision, exposes net deltas since a reviewer last looked, supports arbitrary file revision bounds, and keeps discussions across revisions. GitHub, GitLab, and Bitbucket also track file-level reviewed/viewed state and invalidate it when the file changes.

The LemonCrow distinction is therefore **not** "we remember that something changed."

The stronger, defensible distinction is:

> LemonCrow attaches human judgment to non-overlapping semantic/hunk/file targets with content identity, preserves only judgments whose identity still matches, explicitly reopens changed targets, exposes discarded/unknown state, and conservatively re-resolves comment anchors rather than trusting file- or line-level state.

That is materially finer than file-level review marks and materially more conservative than an AI tool merely doing an incremental re-review.

---

## 2. Platforms covered

### SCM / enterprise review systems

- GitHub Pull Requests
- GitLab Merge Requests
- Bitbucket Cloud Pull Requests
- Azure Repos Pull Requests
- Gerrit Code Review

### Review workflow overlays

- Graphite
- Reviewable

### AI review systems

- GitHub Copilot code review
- CodeRabbit
- Qodo
- Greptile
- Bito
- Graphite Agent, where relevant to Graphite's review experience

### Deliberately not treated as primary 2026 parity targets

Historical or narrower products can still contain useful ideas, but this document does not use them as the main parity bar. The useful architectural research around Hunk, Plannotator and Pyor is already captured in `docs-internal/review-workspace-design.md`.

---

## 3. Current LemonCrow Review baseline

This section is grounded in the implementation, not only the desired spec.

### 3.1 Review identity and human state

Implemented today:

- durable `ReviewSession`;
- immutable `ReviewRevision`;
- persisted raw file / hunk / symbol review units;
- derived **non-overlapping `ReviewTarget`s** for human progress;
- review marks bound to underlying unit identity/fingerprint;
- states:
  - `unreviewed`;
  - `reviewed`;
  - `needs_changes`;
  - `changed_since_review`;
  - `unknown`;
- no durable "visited means reviewed" shortcut;
- review progress over targets, not files, line count, comments or overlapping semantic units;
- safe server-side file remainder completion that rechecks current target eligibility before marking anything.

Key implementation/spec references:

- `src/lemoncrow/pro/capabilities/review/targets.py`
- `src/lemoncrow/pro/capabilities/review/store.py`
- `src/lemoncrow/pro/capabilities/review/sources/local.py`
- `src/lemoncrow/pro/capabilities/review/api.py`
- `docs/planning/2026-09-12-review-reader-spec.md`

### 3.2 Revision reconciliation

Implemented today:

- human marks survive revisions only when the reviewed identity still matches;
- changed targets reopen visibly;
- removed judgments are not silently treated as current;
- revision delta separates:
  - preserved;
  - reopened;
  - added;
  - removed;
  - carried-pending work;
- discarded verdicts remain explainable;
- local source probing waits for a settled fingerprint instead of refreshing while an agent is halfway through writing;
- local incremental refresh preserves unchanged loaded patch content and invalidates only the paths whose source changed;
- automatic refresh and manual refresh share the same revision/reconciliation path.

The local reader therefore behaves more like a live review of an evolving agent workspace than a static PR diff.

### 3.3 Continuous manual review UX

Implemented today in `frontend/src/review/`:

- continuous virtualized diff stream;
- compact review outline grouped by review state/attention;
- split/unified diff;
- syntax themes;
- collapse/expand file;
- file target counts;
- direct dedicated-tab opening for a file/surface;
- target navigation and manual-scroll target ownership;
- recommended or file order;
- large-review lazy patch loading and bounded prefetch;
- search across path, symbol, target metadata, attention reasons and already-loaded comment text;
- command palette and complete keyboard shortcut surface;
- focus mode;
- compact change overview;
- explicit Finish Review sheet that does not pretend finishing is approval.

### 3.4 Comments and discussion state

Implemented today:

- line/range comments;
- file comments;
- threaded replies;
- Comment / Request change / Suggestion dispositions;
- a Request change may atomically mark its target `needs_changes`;
- resolve/reopen;
- human vs author/agent source identity;
- "author says addressed" is distinct from human resolution;
- author response can put work back in front of the reviewer;
- robust anchor relocation;
- relocation method is visible on the comment;
- ambiguous relocation can remain orphaned rather than silently moving the comment.

Important gap: LemonCrow's `Suggestion` is a review disposition today, **not yet an executable replacement patch** comparable to GitHub/GitLab/Gerrit suggestions.

### 3.5 Impact, provenance, verification and evidence

Implemented today:

- changed-symbol analysis;
- out-of-patch callers/impact;
- deterministic attention reasons;
- exact or qualified author/agent provenance;
- evidence freshness;
- file-scoped verification projected to target eligibility only when attribution is honest;
- unknown/not-run remains unresolved rather than becoming pass;
- evidence and visual artifacts in Context;
- current vs stale evidence;
- review overview containing change story, conceptual changes, verification, provenance and artifact state;
- structured feedback export/delivery, including direct delivery to an exact originating agent session when provenance permits it.

### 3.6 Review surfaces beyond a source diff

The current reader/runtimes support more than ordinary source-code diffing:

- rendered Markdown with Preview / Compare / Source;
- web review surfaces;
- API/service review surfaces;
- Bruno-discovered API surfaces;
- configurable runtime setup;
- Docker Compose runner;
- command runner;
- HTTP service runner;
- extensible entry-point runners;
- associated evidence and previews in the same review workflow.

Relevant implementation:

- `frontend/src/review/MarkdownReviewPreview.tsx`
- `frontend/src/review/WebReviewPreview.tsx`
- `frontend/src/review/ApiReviewPreview.tsx`
- `src/lemoncrow/pro/capabilities/review/surface_providers.py`
- `src/lemoncrow/pro/capabilities/review/runtimes.py`
- `src/lemoncrow/pro/capabilities/review/review_setup.py`

### 3.7 Security/privacy of the local reader

The local workspace:

- binds loopback only;
- uses a per-process bearer token;
- sends that token to the browser in a URL fragment rather than a request URL;
- serves the built review application, not the repository as static files;
- refuses arbitrary host exposure.

This matters because `lc review` can be useful before anything is pushed to a remote code host.

---

## 4. Platform landscape: what each system is actually good at

This is intentionally about distinctive product strengths rather than marketing checklists.

### 4.1 GitHub Pull Requests

GitHub is the baseline collaboration/merge surface:

- inline, multi-line and file-level discussion;
- pending review comments and final Comment / Approve / Request changes outcome;
- file-level **Viewed** progress;
- suggested changes and batch application;
- required reviews, CODEOWNERS, branch protection and rulesets;
- checks/statuses and conversation-resolution merge requirements;
- merge queue;
- stacked pull requests are now a public-preview native workflow;
- Copilot can provide automated AI review and optionally re-review new pushes.

**What matters for LemonCrow:** suggestion application, host policy integration, reviewer ownership, and publishing a review outcome. Do not copy GitHub's file-level Viewed state as LemonCrow's source of truth.

### 4.2 GitLab Merge Requests

GitLab has a particularly complete integrated review + policy workflow:

- assigned reviewers and explicit review status;
- pending review comments submitted together;
- Approve / Comment / Request changes;
- file Viewed state invalidated when contents change;
- suggestions that can be applied individually or batched into a commit;
- approval rules and CODEOWNERS;
- merge checks and enterprise policy controls;
- review from web, VS Code or CLI.

**What matters for LemonCrow:** executable/batched suggestions and reviewer/policy adapters. GitLab's merge-policy machinery belongs in GitLab, not inside LemonCrow.

### 4.3 Bitbucket Cloud

Useful review primitives include:

- line/file review and approvals;
- code suggestions that can be committed;
- **tasks** extracted from review comments as explicit author action items;
- merge checks that can require task completion;
- file Viewed state that resets when later commits modify that file;
- default reviewers / ownership-style routing.

**What matters for LemonCrow:** the explicit action-item concept is worth preserving as a possible projection of `request_change`; however, LemonCrow should not create a second task/project-management system unless a host adapter needs it.

### 4.4 Azure Repos

Azure's differentiator is policy-heavy enterprise review:

- required reviewers;
- minimum reviewer counts;
- build/status policies;
- linked-work-item and comment-resolution policies;
- nuanced reviewer votes;
- policies for what happens when new source changes arrive:
  - retain votes;
  - require approval on the last iteration;
  - require approval on every iteration;
  - reset approval votes;
  - reset all reviewer votes.

**What matters for LemonCrow:** Azure demonstrates that "what review remains valid after another iteration?" is an enterprise policy concern, but it operates at reviewer-vote/iteration granularity. LemonCrow can remain finer and content-based while exposing policy adapters in hosted mode.

### 4.5 Gerrit

Gerrit is still the strongest reference here for explicit review process state:

- first-class patch sets;
- arbitrary patch-set A/B comparison;
- review labels / submit requirements;
- per-file reviewed state;
- draft comments published as a review;
- unresolved/resolved comments;
- suggested fixes;
- checks tab;
- file comments;
- rich diff preferences;
- an **Attention Set** whose purpose is literally to model "whose turn is it?" between author and reviewers.

**What matters for LemonCrow:** patch/revision comparison and attention/turn ownership are excellent fits. Gerrit's full change/submit system is not something LemonCrow should recreate.

### 4.6 Reviewable

Reviewable is the closest existing reference for durable human review state across revisions:

- file-level marks are recorded per reviewer and revision;
- net deltas since a reviewer last looked survive rebases/amends;
- arbitrary revision bounds per file;
- a revision/file history matrix;
- discussions stay until explicitly resolved rather than disappearing because code changed;
- discussion **dispositions** are separate from resolution;
- a discussion may separately be "unreplied for" a particular participant;
- custom review completion conditions;
- designated reviewers and scoped reviews such as security/accessibility;
- CODEOWNERS can feed reviewer designation.

**What matters for LemonCrow:** this invalidates any broad claim that revision-aware human review state is unique. It also gives LemonCrow two particularly useful ideas to adopt in its own model: richer turn ownership and an accessible revision history/comparison UI.

**Where LemonCrow is still different:** Reviewable's visible review-progress unit is fundamentally a file/revision. LemonCrow already has content-bound non-overlapping semantic targets, deterministic impact/provenance/verification, local evolving workspaces and rich executable/rendered review surfaces.

### 4.7 Graphite

Graphite's strength is developer workflow around GitHub:

- code-first PR review UI;
- comments outside only-the-changed-line restriction;
- pending-review workflow;
- stack navigation and stack authoring;
- a very strong **PR Inbox** organized around Needs your review, Returned to you, Waiting for review, Approved, etc.;
- stack-aware merge queue;
- AI review capabilities as another layer.

**What matters for LemonCrow:** the inbox is a good model for hosted review discovery/attention. Stack context is worth displaying when supplied by the host, but LemonCrow should not compete with Graphite on stack creation, cascading rebase or merge queue.

### 4.8 CodeRabbit

CodeRabbit is one of the closest competitors on the "help a human understand a large AI-generated change" problem:

- AI-generated walkthrough and review;
- incremental re-review of changes since its last review;
- **Change Stack** (released May 7, 2026) reorganizes a PR into ordered cohorts/layers tied to line ranges rather than leaving it as a flat file list;
- range-specific summaries and optional generated diagrams;
- comments and approvals flow back to GitHub.

**What matters for LemonCrow:** attention ordering is not enough to differentiate LC, and semantic/grouped walkthroughs are no longer unusual. LemonCrow's durable human target state, reconciliation and evidence model must remain the stronger story.

Do not copy AI-generated "logical layers" as opaque truth. LemonCrow's recommended order should remain explainable by concrete deterministic reasons, with optional AI explanation only where useful.

### 4.9 GitHub Copilot code review

Copilot now supports:

- automatic PR review;
- optional automatic re-review on new pushes;
- custom review instructions;
- AI findings/suggestions integrated directly into GitHub review workflow.

GitHub's docs also explicitly note that a Copilot re-review may repeat comments that were previously resolved/downvoted.

**What matters for LemonCrow:** LemonCrow should be able to ingest Copilot findings as advisory source-labelled annotations/evidence. Its own human discussion/reconciliation state should remain separate from an AI review run.

### 4.10 Greptile

Greptile focuses on repository-wide AI context:

- builds a codebase graph;
- reviews PRs with context beyond the changed files;
- exposes inline findings and suggested fixes;
- supports custom rules/context;
- can hand a finding directly to Claude Code, Codex, Cursor, Devin and other agents through **Fix with your Agent**;
- supports GitHub/GitLab and self-hosted/enterprise deployment.

**What matters for LemonCrow:** "whole-codebase impact" and "send feedback to an agent" are competitive areas now, not unique category claims. LC's advantage should be exact provenance when available, deterministic impact evidence, and the closed human-judgment/revision loop.

### 4.11 Qodo

Qodo v2, released February 4, 2026, emphasizes:

- multi-agent review;
- rule enforcement;
- context-aware PR feedback;
- governance around review behavior.

**What matters for LemonCrow:** organization review policy/rules are a credible hosted-enterprise requirement. Do not respond by creating another autonomous multi-agent reviewer; map deterministic policy/check results into LemonCrow evidence/Finish semantics and allow external AI reviewers as inputs.

### 4.12 Bito

Bito combines:

- repository-context AI review using symbol indexing/AST/embeddings;
- incremental reviews on new commits;
- custom guidelines and filtering;
- review analytics;
- GitHub/GitLab/Bitbucket integrations;
- local/IDE/CLI review, including uncommitted changes and commit ranges;
- upstream/downstream impact context for supported languages.

**What matters for LemonCrow:** local review and semantic impact cannot be positioned as unique by themselves. The differentiator is what LC does with human review state after obtaining that context.

---

## 5. Capability parity matrix

Legend:

- **YES** — first-class/current capability.
- **PARTIAL** — some of the capability exists but not the full comparable workflow.
- **HOST** — normally delegated to the Git host rather than a separate review model.
- **NO** — absent.
- **N/A** — not a meaningful goal for that product class.

For compactness, `Major hosts` means GitHub/GitLab/Bitbucket/Azure/Gerrit where the capability is broadly native; exceptions are called out in notes.

| Capability | LemonCrow now | Major hosts | Reviewable | Graphite | AI reviewers | LC product decision |
| --- | --- | --- | --- | --- | --- | --- |
| Inline/range comments | YES | YES | YES | YES | YES | parity achieved |
| File comments | YES | YES broadly | YES | YES | varies | parity achieved |
| Thread replies + resolve/reopen | YES | YES | YES | YES | varies | parity achieved |
| Pending review then publish | PARTIAL: feedback preview/delivery, comments save immediately | YES | YES | YES | N/A | useful but not urgent locally; host adapter can publish as one review |
| Whole-change Approve / Request changes | NO by design; target `needs_changes`, Finish != approval | YES | YES / completion | YES through GitHub | bots may issue host reviews | keep human target judgment separate; publish an explicit host outcome later |
| File Viewed/reviewed tracking | superseded by target state | YES in several hosts | YES, revision-aware | HOST | not core | do **not** regress to file marks as source of truth |
| Semantic symbol/hunk/file judgment targets | **YES** | NO | NO: file/revision | NO | CodeRabbit has guided line-range cohorts, not equivalent human state | **DEFEND** |
| Non-overlapping progress denominator | **YES** | NO: mostly files/PR approval | NO: files | NO | NO | **DEFEND** |
| Content-bound target judgment across revisions | **YES** | coarse vote/file invalidation | file/revision | HOST | incremental AI run != human judgment | **DEFEND** |
| Preserve unchanged human judgments while reopening only changed target content | **YES** | PARTIAL/coarse | PARTIAL at file/revision level | HOST | incremental AI review is a separate bot run | **DEFEND** |
| Explicit preserved/reopened/new/removed/carried-pending revision delta | **YES** | NO equivalent target projection | PARTIAL via matrix/net delta | NO | new-delta review exists, but not human-judgment reconciliation | **DEFEND** |
| Arbitrary revision A/B comparison | PARTIAL: revisions/history exist; current reader is optimized around current+delta | Gerrit YES; hosts have commit/diff history | YES | HOST | generally NO | **ADD** |
| Revision/file history matrix | NO | Gerrit/host history varies | YES | NO | NO | **ADD later**, likely target-history rather than copying file matrix literally |
| Conservative comment relocation with visible relocation method | **YES** | varying/outdated-thread behavior | YES mapping, less explicit to reviewer | HOST | usually host comments | **DEFEND** |
| Explicit orphaned comment rather than silent relocation | **YES** | varies | discussions persist | HOST | host-dependent | **DEFEND** |
| Author says addressed but human still owns resolution | **YES** | conversation resolution usually shared/manual | dispositions/unreplied model is richer | HOST | bots may auto-resolve own comments | **DEFEND**, then add explicit turn ownership |
| Discussion waiting-on-author / waiting-on-reviewer | PARTIAL | Gerrit Attention Set strong | YES (`unreplied for`) | inbox status gives workflow-level signal | varies | **ADD** |
| Executable suggested patch | **NO** | GitHub/GitLab/Bitbucket/Gerrit YES | host integration | HOST | many YES | **ADD NOW** |
| Batch apply suggestions | NO | GitHub/GitLab YES | host | HOST | varies | **ADD with executable suggestions** |
| Review task/action item | Request change is semantically close, but no task object | Bitbucket strong; issues/tasks vary | disposition | HOST | findings act as tasks | **do not build PM subsystem**; expose/export Request change as host task if needed |
| CODEOWNERS / designated reviewers | NO local multi-user model | YES | YES + scoped designated reviewers | HOST/GitHub | policy configs vary | **ADD HOSTED via import/adapters** |
| Scoped reviewer roles (`security`, `API`, etc.) | NO | approval-rule variants | YES | not core | specialized AI roles exist | **ADD HOSTED** if demanded; fits target ownership well |
| Required approvals / completion policy | NO local; Finish is informational | YES | YES custom completion | HOST | Qodo/policies vary | **INTEGRATE**, do not replace host merge policy |
| Review inbox | NO | host lists/notifications | GitHub-linked | **YES strong** | dashboards vary | **ADD HOSTED** |
| Explicit attention/whose-turn state | PARTIAL at comment response level | Gerrit **YES strong** | YES discussion participants | inbox-level workflow | varies | **ADD HOSTED**, also useful for author-agent loops |
| Stack navigation | NO | GitHub public preview; Gerrit dependencies differ | GitHub only | **YES strong** | host-dependent | **DISPLAY/INTEGRATE later** |
| Stack creation/rebase/merge queue | NO | host-dependent | NO | **YES strong** | NO | **IGNORE as LC-owned functionality** |
| Branch protection / merge checks | NO | **YES** | consumes GitHub state | consumes GitHub | N/A | **IGNORE as LC-owned functionality; ingest status** |
| CI/check state | YES as verification evidence when provided | YES | consumes host | consumes host | often consumes host/tools | parity conceptually; continue evidence adapters |
| File-scoped check → target eligibility | **YES** when attribution is explicit | host checks usually PR/commit-level | not equivalent | host | findings/checks vary | **DEFEND** |
| First-class unknown/not-run/degraded evidence | **YES** | status systems distinguish pending/fail, but not LC's evidence semantics | PARTIAL | HOST | confidence often model-specific | **DEFEND** |
| Out-of-patch callers / semantic impact | **YES deterministic** | not normal manual-review primitive | NO | not core | Greptile/Bito and others provide codebase context/impact | keep, but do not claim category uniqueness |
| Explainable recommended human review order | **YES deterministic reasons** | mostly file order | review state rather than semantic order | code-first UI | CodeRabbit Change Stack / AI walkthroughs | **DEFEND explainability + human-state coupling** |
| Opaque AI risk/effort score | deliberately NO | NO core | NO | AI layer may | common in AI products | **IGNORE by default** |
| Change overview/summaries | YES compact | YES descriptions/summaries | YES context | YES | YES heavily | parity achieved; keep secondary to code |
| Generated sequence/ER diagrams | NO | NO core | NO | AI layer may | CodeRabbit/Greptile variants | **DEFER/on-demand only**, not reader chrome |
| AI autonomous review generation | deliberately not LC's core | Copilot optional | NO | Agent optional | **YES** | **do not rebuild**; ingest external findings instead |
| External AI finding ingestion into one human review state | source-labelled annotation model can represent it; adapters incomplete | host comments | GitHub sync | GitHub | producers, not aggregators | **ADD adapters**; high leverage |
| Direct feedback/fix handoff to coding agent | **YES with exact provenance** | Copilot-agent workflows | NO | AI integrations vary | Greptile and others now do this | keep improving; provenance/closed-loop is the differentiator |
| Exact author/agent provenance | **YES where recorded** | commit author != execution provenance | NO | HOST | generally bot context, not author execution ledger | **DEFEND** |
| Evidence of what agent inspected/tested | **YES when captured** | NO | NO | NO | generally reviewer context, not author execution provenance | **DEFEND** |
| Review uncommitted/staged local work manually | **YES** | PR products generally NO; tools can check out locally | NO | PR-centric | Bito/local AI products YES for AI review | keep; useful but not unique across all tooling |
| Incremental live review of evolving local workspace | **YES** local milestone | hosted push/patchset model | hosted revisions | hosted PR updates | incremental bot reruns | **DEFEND human-state semantics** |
| Rendered Markdown review in reader | **YES** | host rendering varies | source-centric | source-centric | mostly source comments | keep |
| Web before/after/live surface review | **YES** | external deployment preview usually separate | NO | NO | generally NO | **DEFEND** |
| API/service/Bruno review surface | **YES** | checks/tools separate | NO | NO | generally source-focused | **DEFEND** |
| Screenshots/traces/video/evidence bound to revision | **YES** | attachments/check artifacts exist but separate | NO comparable integrated model | NO | varies | **DEFEND** |
| Large-review bounded lazy diff loading | **YES** | mature hosts optimize rendering | mature SaaS | mature SaaS | N/A | parity requirement, not marketing differentiation |
| Review analytics | NO reader product | host analytics vary | some workflow state | Graphite strong | Bito etc. strong | **DEFER until hosted usage exists** |
| Enterprise review-rule governance | partial deterministic detectors, no org review-policy product | GitLab/Azure/GitHub strong | custom completion | host | Qodo/Greptile/Bito strong AI-rule config | **ADD HOSTED carefully**, focused on policy/evidence rather than prompt sprawl |

---

## 6. What is LC-only or materially stronger among the reviewed set

"LC-only" here means **not found as an equivalent first-class capability in the platforms researched above as of 2026-09-15**. It does not claim that no niche or internal system anywhere has ever implemented the idea.

### 6.1 Semantic human judgment targets, not semantic AI findings

Many products understand symbols. Many AI reviewers use ASTs, graphs or line-range cohorts.

The unusual LC combination is that **the semantic unit is the durable unit of human judgment**:

```text
changed source
  -> raw file/hunk/symbol identities
  -> non-overlapping ReviewTargets
  -> human judgment
  -> content fingerprint
  -> next revision reconciliation
```

A symbol spanning multiple diff hunks can still be one human target. Uncovered source falls conservatively to hunk/file targets rather than being omitted or double-counted.

This is substantially different from:

- a file Viewed checkbox;
- an approval vote;
- an AI issue anchored to a line range;
- an AI incremental review of the latest commits.

### 6.2 Content-precise human frontier

Reviewable proves the market value of revision-aware review state, but LC takes the frontier below file granularity and ties it to content identity.

LC can say:

```text
3 judgments are still valid
1 target changed and reopened
2 targets are new
1 old target disappeared
551 targets were already pending
```

without turning the whole changed file back into one undifferentiated review unit.

This should be one of the central product stories.

### 6.3 Conservative uncertainty is part of the product model

LC does not silently convert missing evidence into a positive claim:

- unknown fingerprint != reviewed;
- unknown caller count != zero callers;
- not-run test != pass;
- unobserved author read != author skipped file;
- ambiguous relocated comment != exact anchor;
- stale artifact != current verification.

Most competing systems have status/confidence concepts, but LC applies this trust model across review identity, impact, provenance, verification and comment relocation.

### 6.4 Visible, conservative annotation relocation

The important difference is not just "comments survive updates."

LC exposes **how** a comment found its current anchor and permits an orphaned state when it cannot make the claim safely. This makes comment persistence auditable rather than magical.

### 6.5 Human judgment + author/agent response are separate authorities

An author or agent can say a change request was addressed, but cannot mark the human's concern resolved for them.

That separation is especially important when the author is an automated coding agent.

Reviewable has related participant/disposition concepts and Gerrit has explicit attention routing, so the broad workflow idea is not unique. LC's specific integration with durable semantic targets and an originating agent loop is the stronger combination.

### 6.6 Exact execution provenance inside manual review

When LemonCrow captured it, the reviewer can inspect facts about the authoring agent session rather than infer them from Git authorship:

- who/which agent authored work;
- which exact session;
- what it inspected;
- what it changed;
- what it verified;
- what relevant areas were not observed;
- feedback route back to that session.

AI review products have reviewer context; Git hosts have authors/commits. That is not the same thing as an author execution ledger tied to the review.

### 6.7 One review reader for code plus behavior surfaces

The current LC direction combines human source review with:

- rendered Markdown;
- web surfaces;
- API/service surfaces;
- screenshots/traces/artifacts;
- verification;
- source impact;
- human marks.

A Git host may link checks/deploy previews, but the review-state model normally remains a source diff. LC can make "review what changed" include the behavior the code produces.

### 6.8 Local evolving-workspace review with preserved human state

AI tools can review uncommitted code, and hosted systems can review successive pushes. The LC-specific value is the **manual human review state surviving a live local agent iteration** through the same content-reconciliation semantics used for durable revisions.

This becomes especially important for hosted coding agents later: the reviewer should not need a PR push merely to get a stable human review loop.

---

## 7. What competitors have that is worth adding

Priorities here mean product priority relative to Review, not an engineering estimate.

## P0 — close these in the reader model

### P0.1 Executable suggested changes

**Seen in:** GitHub, GitLab, Bitbucket, Gerrit, multiple AI reviewers.

Current LC `Suggestion` comments do not contain an executable replacement.

Add a structured suggestion object such as:

```text
ReviewSuggestion
  id
  review_id
  revision_id
  annotation_id
  target_unit_key
  path
  old_range / anchor
  expected_content_fingerprint
  replacement_text
  state: proposed | applied | stale | rejected | reverted
  author/source
```

Requirements:

- never apply against stale content;
- preview exact patch before apply;
- apply one or batch compatible suggestions;
- mutation creates normal source change, then the existing revision path detects/reconciles it;
- preserve attribution;
- support undo/revert where the workspace still permits it;
- host adapter may publish native suggestion syntax when possible;
- an AI-generated suggestion is still advisory and never a human review mark.

Why P0: it closes the most obvious manual-review interaction gap without changing LC's product boundary.

### P0.2 Explicit conversation turn ownership

**Seen strongest in:** Gerrit Attention Set and Reviewable `unreplied for`/dispositions.

Today LC has useful pieces:

- open/resolved;
- Request change;
- replies;
- `author says addressed`;
- re-review remains human-owned.

Add a derived or explicit workflow field that can answer:

```text
waiting on author/agent
waiting on reviewer
no response required
```

Do **not** overload `resolved`; resolution and turn ownership are different facts.

This immediately improves local human↔agent review and later becomes the primitive for a hosted inbox.

### P0.3 Revision compare / judgment history

**Seen strongest in:** Gerrit and Reviewable.

LC already stores immutable revisions and revision deltas. Expose that substrate.

Minimum useful UI:

- current vs previous revision;
- choose any two review revisions;
- for a target, show mark history and fingerprint transitions;
- show comment anchor transition/history;
- explain why a prior judgment was preserved/reopened/discarded.

Do not copy Reviewable's file matrix literally. LC's natural history view should be **target/judgment-centric**.

## P1 — add with hosted multi-user review

The hosted product adds durable review identity, historical revisions, participant/request workflows, Comments/History surfaces and enterprise authorization. Detailed hosted implementation design is intentionally maintained in the private product repository; this section remains the public competitive-priority summary.

### P1.1 Review inbox + attention routing

**Seen strongest in:** Graphite PR Inbox and Gerrit Attention Set; hosts also have review-request lists.

Suggested hosted sections:

```text
Needs my review
Returned to me
Waiting on author/agent
Waiting on reviewer
Changed since my review
Ready / no human work pending
Finished recently
```

The inbox should be projected from LC review state + host metadata, not invent another state machine.

### P1.2 Review requests, identity and reviewer scopes

**Seen in:** GitHub/GitLab CODEOWNERS and approvals, Reviewable designated/scoped reviewers, Azure reviewer policies.

Hosted LC will need:

- reviewer identity;
- requested reviewers;
- target/file scope;
- optional review focus such as `security`, `API`, `UX`, `data`;
- ownership imported from CODEOWNERS or provider policy;
- independent per-reviewer marks where multiple humans review the same target.

Important architectural consequence: local single-human state must not be casually generalized by adding one global `reviewed_by` list. Multi-reviewer completion is a projection over per-actor judgments.

### P1.3 Host review publication adapters

LC should be able to publish to GitHub/GitLab/etc.:

- pending comments as one submitted review where the provider supports it;
- Comment / Approve / Request changes **only when the human explicitly chooses that host outcome**;
- native suggestions where representable;
- resolved state where safe;
- links back to richer LC evidence when the host model cannot represent it.

Do not infer "Approve" from `all targets reviewed`. Finished human reading and SCM merge approval are deliberately separate concepts.

### P1.4 External automated-review ingestion

Rather than building another general AI review bot, add adapters for:

- GitHub Copilot review;
- CodeRabbit;
- Qodo;
- Greptile;
- Bito;
- generic GitHub/GitLab bot review comments/checks.

Normalize them through the existing unified annotation/evidence model:

```text
source = ai | tool
source_id = coderabbit | copilot | greptile | ...
state != human judgment
confidence/evidence retained where supplied
```

Then the human can use one Review Reader over many automated signals without giving those systems authority over LC's review frontier.

This is strategically better than trying to beat every AI reviewer at issue generation.

### P1.5 Organization review policy as evidence/completion context

**Seen in:** GitHub/GitLab/Azure policies, Reviewable completion scripts, Qodo/Greptile/Bito custom review rules.

Hosted LC should understand policy such as:

- required owner review;
- required security review for certain paths;
- required checks;
- unresolved Request change blockers;
- verification requirements by target/path type.

Prefer deterministic policy facts over prompt-only "review harder here" instructions.

LC Finish should explain unmet policy, but host merge policy remains authoritative for whether a PR may merge.

## P2 — useful after hosted review is operating at scale

### P2.1 Target-level coverage overlay

Reviewable can ingest coverage visualization; hosts surface coverage via checks. LC already has target-scoped verification semantics.

A future LC extension could associate current line/branch coverage with targets, but only when the coverage artifact has trustworthy source-revision identity.

### P2.2 Review analytics

Graphite/Bito and Git hosts offer analytics. Useful eventual metrics:

- time from review request → first human judgment;
- time waiting on author vs waiting on reviewer;
- reopened target rate;
- fraction of judgments preserved across agent iterations;
- comment/address/re-review loop count;
- review targets per conceptual change;
- verification failure caught before finish.

Do not optimize for vanity metrics such as lines reviewed or raw comments generated.

### P2.3 Stack context navigation

When a provider supplies stack relationships, show:

- previous/next stack item;
- dependency on lower changes;
- review/verification status of adjacent stack items.

Do not make LC responsible for stack creation, branch movement or cascading rebase.

### P2.4 On-demand diagrams

CodeRabbit/Greptile demonstrate demand for sequence diagrams and architectural summaries.

If added, keep them:

- on demand;
- evidence-linked;
- secondary to source/behavior;
- explicitly generated, not authoritative.

They should not become permanent reader chrome or feed the review frontier unless derived deterministically from trustworthy structure.

---

## 8. What to ignore completely as LC-owned product functionality

These are useful capabilities, but mature systems already own them and rebuilding them would dilute Review.

### 8.1 Repository hosting

Do not build:

- Git remote hosting;
- fork management;
- repository permissions;
- branch management as a product.

### 8.2 Merge queue / merge train

GitHub, GitLab, Graphite and others already solve this in the system that owns the protected branch and CI identity.

LC should consume:

```text
mergeable?
checks current?
queue state?
policy blockers?
```

but not become the queue.

### 8.3 Branch protection / merge policy engine replacement

LC may project enterprise review policy and explain it, but the SCM remains the final authority for merge admission.

### 8.4 Stack authoring and cascading rebase

Graphite and now GitHub invest heavily here. It is not LC's review moat.

Integrate stack metadata later; do not build another `gt`/`gh stack`.

### 8.5 Generic issue tracker / project manager

A Request change can export into a Bitbucket task, GitHub issue, Jira ticket, etc. when the user asks.

Do not create another project-management database just because comments sometimes imply follow-up work.

### 8.6 Full IDE / source editor

Narrow mutation is justified for **Apply suggestion** because it closes a review action.

General editing, navigation, language-server UX and coding belong to the user's editor/agent.

### 8.7 Real-time multi-user co-editing

Async review state and turn ownership are much more important. Live cursors/presence/co-editing are not needed to make hosted review commercially credible.

### 8.8 Autonomous AI merge approval

Do not let an AI reviewer produce the human `reviewed` state or silently approve the change.

External AI findings may:

- raise attention;
- provide evidence;
- propose suggestions;
- fail a configured machine check if the organization explicitly defines that policy.

They do not become a human judgment.

### 8.9 Another generic AI review essay generator

Copilot, CodeRabbit, Qodo, Greptile, Bito and Graphite already compete aggressively on generating review comments and summaries.

LC should win by **making all of that reviewable by a human efficiently**, not by producing the longest competing review.

### 8.10 Opaque risk/effort scores

Continue the current rule:

```text
public contract changed · 12 callers outside patch
```

is better than:

```text
Risk 87
Review effort 4/5
```

Explainable concrete signals are more useful and easier to audit.

### 8.11 File-level Viewed as the canonical review state

Providers may require/import/export it for compatibility.

Internally it would be a regression from `ReviewTarget`.

A file can be *derived complete* when its current targets are settled; it should not become the atomic persisted judgment merely because GitHub/GitLab use a checkbox.

---

## 9. Competitive position by problem, not vendor

### Problem: "I need normal team PR collaboration"

Best solved by the Git host. LC should integrate.

### Problem: "I need to know whose turn it is"

Gerrit/Reviewable/Graphite have stronger current workflow surfaces. LC should add this to hosted review.

### Problem: "I need executable reviewer fixes"

Major hosts and AI reviewers are ahead. LC should add structured suggestions.

### Problem: "I need to review only what changed since my previous pass"

Reviewable already solves this at file/revision granularity; AI reviewers perform incremental bot reruns; Git hosts reset coarse review state.

LC's opportunity is to make this **more precise and safer at human semantic-target granularity**.

### Problem: "I need help understanding a giant AI-generated change"

CodeRabbit Change Stack, Graphite, Greptile, Bito and other AI tools already attack orientation/order/context.

LC cannot rely on "we order files intelligently" as the moat.

The stronger story is:

```text
understand -> judge -> agent changes -> preserve valid judgment -> re-review only invalidated/new work
```

with deterministic evidence and rich behavior surfaces.

### Problem: "I want AI to find bugs"

Crowded market. Integrate rather than center LC on this battle.

### Problem: "I want to trust that I personally reviewed what is now shipping"

This is where LC is strongest.

The key evidence chain is:

```text
current code identity
  + my current target judgments
  + reopened invalidated judgments
  + unresolved comments
  + current verification
  + author/agent provenance
  + behavior evidence
  + explicit unknowns
```

That is the product thesis worth defending.

---

## 10. Recommended product boundary after parity review

Use this as the default decision test for future Review requests.

### Build in LemonCrow when the feature answers one of these

- What exactly did the human review?
- Is that judgment still valid after the next revision?
- What specifically changed since that judgment?
- What should the human inspect next, and why?
- What evidence/context is needed to judge this target?
- Who/what authored the work and what did it verify?
- Is a comment still anchored honestly?
- Whose turn is this review conversation?
- How does the reviewer send precise feedback/fixes back to the author/agent?
- What behavior/artifact changed in addition to source text?
- What remains unresolved when the reviewer stops?

### Integrate rather than build when the feature answers one of these

- Who owns this repository/path according to the company?
- Which PR/MR requests my review?
- What host approval is required?
- Are branch checks satisfied?
- Is this change in a stack?
- What AI-review tools found issues?
- What is the merge-queue status?

LC may provide a better unified view, but the authoritative underlying fact belongs elsewhere.

### Leave entirely to another product when it answers one of these

- Where is the Git repository hosted?
- How are branches/forks administered?
- How are stacks rebased?
- In what order does the protected branch merge queued PRs?
- How does the team manage Jira/project tasks?
- Which full IDE edits/builds the code?

---

## 11. Proposed implementation order

This is intentionally small. Do not turn the parity exercise into 30 new features.

**Enterprise-hosted priority correction (2026-09-15):** for enterprise adoption, durable review identity/navigation, Comments, historical revisions/events, hosted identity/RBAC and multi-person requests take precedence over convenience features such as executable suggestions.

### Track A — finish the local/manual reviewer loop

1. **Stable Review ID/list/history navigation + all-comments surface** (shared with hosted H1).
2. **Revision compare + target judgment history** (shared with hosted H2).
3. **Conversation turn ownership**.
4. **Executable suggestions**.

These make the existing local review model materially more complete while building the exact contracts hosted Review needs later.

### Track B — hosted collaboration

After the hosted review source/storage/transport is operational:

4. **Per-actor review marks / reviewer identity model**.
5. **Review requests + Inbox + Attention state**.
6. **CODEOWNERS/provider ownership import and scoped reviewer requests**.
7. **GitHub/GitLab review publication adapters**.
8. **External AI reviewer ingestion adapters**.

### Track C — enterprise polish after real usage

9. **Policy/completion projection**.
10. **Review analytics based on target/revision state**.
11. **Stack context navigation**.
12. Optional target coverage/on-demand diagrams only if customer use proves them valuable.

---

## 12. Concrete acceptance criteria for the next three additions

### 12.1 Executable suggestion

A suggestion is shippable only if:

- it is bound to the frozen review revision and target/anchor;
- stale content refuses application;
- the reviewer can preview the exact resulting patch;
- applying it mutates source through a bounded review action rather than creating a general editor;
- the resulting source change creates/refreshes a normal ReviewRevision;
- existing reconciliation reopens/preserves judgments normally;
- batch apply is atomic or explicitly reports partial refusal;
- provenance identifies the suggestion author/source;
- undo is supported where source/workspace state makes it safe.

### 12.2 Turn ownership

Turn state is shippable only if:

- resolution remains independent;
- a human Request change can become `waiting_on_author`;
- an exact author/agent response can become `waiting_on_reviewer`;
- the author cannot mark the human concern resolved;
- generic comments can remain `no_response_required`;
- revision refresh cannot accidentally clear the turn;
- later hosted inbox projection requires no second state model.

### 12.3 Revision history/compare

Revision compare is shippable only if:

- any two persisted review revisions can be selected;
- target history shows mark/reopen/discard transitions honestly;
- comment relocation history preserves exact/heuristic/orphan distinctions;
- the current review frontier remains based only on the latest revision;
- looking at an old revision cannot mutate current judgment accidentally;
- large-review loading remains bounded.

---

## 13. Research sources

Research used official product documentation wherever available. Features and product tiers change; re-check provider docs before implementing a provider-specific adapter.

### GitHub

- Reviewing proposed changes: https://docs.github.com/en/enterprise-cloud@latest/pull-requests/how-tos/review-pull-requests/reviewing-proposed-changes-in-a-pull-request?tool=webui
- Pull-request review feature index: https://docs.github.com/en/pull-requests/how-tos/review-pull-requests
- CODEOWNERS: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners
- Protected branches: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches
- Stacked pull requests: https://docs.github.com/en/pull-requests/get-started/about-stacked-prs
- Merging stacked pull requests: https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/merging-stacked-pull-requests
- Copilot code review: https://docs.github.com/en/copilot/concepts/agents/code-review
- Using Copilot code review: https://docs.github.com/en/copilot/how-tos/use-copilot-agents/request-a-code-review/use-code-review

### GitLab

- Merge request reviews: https://docs.gitlab.com/user/project/merge_requests/reviews/
- Changes / Viewed state: https://docs.gitlab.com/user/project/merge_requests/changes/
- Suggestions: https://docs.gitlab.com/user/project/merge_requests/reviews/suggestions/
- Approvals: https://docs.gitlab.com/user/project/merge_requests/approvals/

### Bitbucket Cloud

- Review code in a pull request: https://support.atlassian.com/bitbucket-cloud/docs/review-code-in-a-pull-request/
- Merge checks: https://support.atlassian.com/bitbucket-cloud/docs/suggest-or-require-checks-before-a-merge/

### Azure Repos

- Pull requests: https://learn.microsoft.com/en-us/azure/devops/repos/git/about-pull-requests?view=azure-devops
- Branch policies: https://learn.microsoft.com/en-us/azure/devops/repos/git/branch-policies?view=azure-devops

### Gerrit

- Review UI: https://gerrit-review.googlesource.com/Documentation/user-review-ui.html
- Attention Set: https://gerrit-review.googlesource.com/Documentation/user-attention-set.html
- Patch sets: https://gerrit-review.googlesource.com/Documentation/concept-patch-sets.html

### Reviewable

- Introduction: https://docs.reviewable.io/
- Files / revisions / review marks: https://docs.reviewable.io/files
- Discussions / dispositions: https://docs.reviewable.io/discussions
- Reviews / completion: https://docs.reviewable.io/reviews
- Repository completion conditions: https://docs.reviewable.io/repositories

### Graphite

- Reviewing pull requests: https://graphite.com/docs/review-pull-requests
- PR Inbox: https://graphite.com/docs/use-pr-inbox
- Merge Queue: https://graphite.com/docs/graphite-merge-queue

### CodeRabbit

- Documentation: https://docs.coderabbit.ai/
- Commands / incremental vs full review: https://docs.coderabbit.ai/guides/commands
- Changelog / Change Stack: https://docs.coderabbit.ai/changelog

### Greptile

- Overview: https://www.greptile.com/docs/introduction
- Anatomy of a review: https://www.greptile.com/docs/code-review/first-pr-review
- Graph-based codebase context: https://www.greptile.com/docs/how-greptile-works/graph-based-codebase-context
- Fix with your Agent: https://www.greptile.com/docs/integrations/fix-with-your-agent

### Qodo

- Qodo v2 code review: https://docs.qodo.ai/code-review

### Bito

- Overview: https://docs.bito.ai/ai-code-reviews-in-git/overview
- Key features: https://docs.bito.ai/ai-code-reviews-in-git/key-features
- Agent configuration/incremental review: https://docs.bito.ai/ai-code-reviews-in-git/install-run-using-bito-cloud/create-or-customize-an-agent-instance
- CLI/local review: https://docs.bito.ai/ai-code-reviews-in-cli/quick-start

---

## 14. Bottom line

The parity exercise does **not** say LemonCrow needs dozens of features.

It says the product boundary is clearer now:

```text
Git host                     LemonCrow Review                         AI/tool ecosystem
--------                     ----------------                         -----------------
PR / MR identity      ->     human ReviewSession          <-          findings
review requests              semantic ReviewTargets                  checks
CODEOWNERS                    durable judgments                       suggestions
branch policy                 revision frontier                       static analysis
checks/statuses               comments + honest anchors               AI reviewers
merge queue                   impact / provenance / evidence          coding agents
merge                          behavior previews
                               feedback loop
```

The important competitive correction is that **revision-aware review is not unique** and **semantic/AI-assisted review ordering is not unique**.

What remains unusually strong is the combination:

> **A human reviews meaningful targets; LemonCrow records exactly what that judgment covered; an agent or author changes the work; LemonCrow preserves only the judgments it can still justify; and the reviewer sees only the new/reopened work together with impact, provenance, verification and behavior evidence.**

Build around that. Add executable suggestions, turn ownership and revision-history UX. Then use hosted integrations for collaboration, ownership, policy and external AI findings instead of rebuilding the surrounding SCM ecosystem.
