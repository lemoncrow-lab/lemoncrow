# LemonCrow Review — Local/Hosted Product UX Execution Ledger

**Status:** ACTIVE  
**Date:** 2026-09-23  
**Branch:** `feat/review-product-ux`  
**Canonical Reader semantics:** `docs/planning/2026-09-12-review-reader-spec.md`  
**Existing Reader execution history:** `docs/planning/2026-09-12-review-reader-execution.md`  
**Polish baseline:** `docs/planning/2026-09-18-review-polish-freeze.md`  
**Hosted collaboration model:** `docs-internal/planning/hosted-review/2026-09-15-hosted-review-management-plan.md`  
**Hosted production infrastructure:** `docs-internal/audits/2026-09-20-hosted-mode-production-readiness.md`

## 0. Goal

Make LemonCrow Review feel like one coherent review product with two deliberately different operating contexts:

- **Local Review:** the fastest path from a developer workspace to a trustworthy human review, with no PR/account/remote requirement.
- **Hosted Review:** the same semantic Review Reader elevated into a durable multi-person collaboration object with identity, attention ownership, policy, provider linkage, sharing, and cross-device history.

The shared target/revision/judgment engine remains authoritative. Do not fork Review semantics between local and hosted. Differences belong in capability discovery, shell/context, entry/inbox workflow, collaboration controls, and persistence/identity affordances.

## 1. Product contract

### Shared core

Both modes keep:

- semantic `ReviewTarget` reading and progress;
- reviewed / needs-changes / changed-since-review / unknown judgment semantics;
- revision reconciliation and history;
- inline comments, requests, suggestions, and change proposals;
- source, impact, checks, evidence, history, and provenance context;
- code + Markdown + web + API + media + service review surfaces;
- finish/readiness flow;
- search, recommended/file ordering, keyboard access, responsive Reader behavior.

### Local-first capabilities

Local Review emphasizes:

- current workspace / branch / staged / unstaged / range identity;
- capture and refresh directly from the workspace;
- no account, PR, remote, or hosted service requirement;
- local execution/surface discovery;
- local durable review history;
- explicit explanation that review state is stored on this machine.

### Hosted-only collaboration capabilities

Hosted Review may add, when advertised by capability:

- organization/repository identity;
- durable shared URL and cross-device history;
- participants and reviewer requests/scopes;
- explicit attention / whose-turn-is-it state;
- independent per-reviewer outcomes and progress;
- provider linkage/publication;
- organization review policy;
- guest/external review and sharing;
- private reviewer drafts;
- audit/activity history.

Do not show hosted collaboration vocabulary in local mode merely for visual parity.

## 2. UX principles for this execution

1. **Mode is obvious but quiet.** One compact context affordance, not banners everywhere.
2. **The diff still owns the screen.** This work must not regress the Review polish freeze.
3. **Progressive disclosure, not Beginner/Expert modes.** Common judgment stays obvious; technical evidence stays nearby but secondary.
4. **Use reviewer language, not engine vocabulary.** Explain `changed_since_review`; do not make users learn the enum.
5. **Every empty/error/degraded state gives the next useful action when one exists.**
6. **Local is a first-class product, not a crippled Hosted tier.**
7. **Hosted is collaboration, not Local plus a People button.**
8. **Capabilities, never hostname heuristics.** The UI does not infer deployment posture from origin, server name, or auth shape.
9. **One fact, one primary place.** Do not duplicate revision/status/count/action chrome.
10. **Preserve expert escape hatches.** Raw source, compare, provenance, diagnostics, diff preferences, exact IDs and provider state remain reachable.

## 3. Execution queue

Status values: `TODO`, `IN PROGRESS`, `DONE`, `BLOCKED`.

| ID | Priority | Work | Status |
| --- | --- | --- | --- |
| RUX-001 | P0 | Machine-readable deployment + Review capability contract | DONE |
| RUX-002 | P0 | Shared frontend `ReviewEnvironment` / capability provider | DONE |
| RUX-003 | P0 | Mode-aware Reader identity/context affordance | DONE |
| RUX-004 | P0 | Local Reviews directory: local workflow semantics + useful rows | DONE |
| RUX-005 | P0 | Hosted inbox: collaboration/attention semantics + scanability | DONE |
| RUX-006 | P0 | First-run and zero-state onboarding for Local and Hosted | DONE |
| RUX-007 | P0 | Reader next-action hierarchy and target-to-target continuation | DONE |
| RUX-008 | P0 | Human-language explanations for changed/unknown/preserved judgment | DONE |
| RUX-009 | P1 | Local workspace state/refresh affordances in the Reader shell | DONE |
| RUX-010 | P1 | Hosted collaboration strip: requester/turn/progress/author response | DONE |
| RUX-011 | P1 | Progressive disclosure audit of context/checks/evidence/provenance/history | DONE |
| RUX-012 | P1 | Reader component-boundary decomposition without visual rewrite | DONE |
| RUX-013 | P1 | Responsive + keyboard + accessibility requalification after shell changes | DONE |
| RUX-014 | P1 | Real-browser local + two-account hosted dogfood and wording pass | DONE |

## 4. Detailed tasks and acceptance criteria

### RUX-001 — Machine-readable deployment + Review capability contract

**Problem**

The common discovery document advertises protocol capabilities but not product deployment posture or Review UX capabilities. Hosted readiness already tracks this gap as HST-002. Frontends must not infer Local/Hosted semantics from hostname, server implementation name, or whether Authward is configured.

**Deliver**

Add a stable discovery projection similar to:

```json
{
  "deployment": {
    "mode": "local | customer_hosted | managed_dedicated | managed_shared"
  },
  "product_capabilities": {
    "review": {
      "workspace_refresh": true,
      "local_workspace": true,
      "collaboration": false,
      "requests": false,
      "participants": false,
      "provider_sync": false,
      "sharing": false,
      "guest_review": false,
      "organization_policy": false,
      "audit_history": false,
      "private_drafts": false
    }
  }
}
```

The exact shape may be smaller initially, but it must be additive, typed, tested, and capability-oriented.

**Acceptance**

- local discovery says `mode=local` and advertises local workspace behavior;
- enterprise discovery reports configured deployment mode rather than relying on server name;
- Review capability booleans are explicit;
- unauthenticated discovery leaks no tenant/repository/user facts;
- protocol negotiation semantics remain unchanged;
- server/client tests cover the wire contract.

### RUX-002 — Shared frontend ReviewEnvironment

**Deliver**

- typed `ReviewEnvironment` in public frontend Review code;
- one fetch/cache path for discovery;
- capability helpers consumed by shared/local/hosted Review UI;
- deterministic fallback for older servers that does not silently invent hosted collaboration.

**Acceptance**

- components do not inspect hostname or enterprise package identity;
- missing capability means feature unavailable/hidden, not guessed;
- environment fetch failure does not make an already-open Review unreadable.

### RUX-003 — Mode-aware Reader identity/context

**Local target**

Compact context communicates: local storage/workspace + source range/branch when available.

**Hosted target**

Compact context communicates: organization/repository/shared review + current attention when available.

**Acceptance**

- mode/context visible without a permanent banner;
- no duplicate review title/revision/status metadata;
- explanation is available on interaction for less experienced users;
- narrow laptop layout keeps the diff dominant.

### RUX-004 — Local Reviews directory

Rows should prioritize:

- review/change title;
- repository + source range/workspace identity;
- progress (`reviewed / total`);
- changed-since-review / needs-changes signal when present;
- updated time;
- obvious `Continue` affordance through row interaction.

Filters remain local lifecycle concepts: Needs review / All / Finished / Archived.

**Acceptance**

- no hosted attention/request vocabulary;
- user can tell which review to continue without opening each row;
- useful narrow-screen presentation;
- search/status behavior remains dense and operational.

### RUX-005 — Hosted inbox

Hosted buckets should answer collaboration questions, not merely lifecycle state:

- Needs me;
- Requested / assigned where applicable;
- Waiting on author;
- Changed since my review;
- Watching / other active;
- Finished/archived through lifecycle filtering.

Use only states that the hosted backend can actually support; do not fabricate buckets.

**Acceptance**

- current-user attention is visually stronger than generic status;
- requester/author/scope can be understood without opening the review when available;
- rows do not become dashboard cards;
- provider metadata remains secondary.

### RUX-006 — First-run and zero-state onboarding

**Local zero state**

Explain `lc review`, what it can review, and that no PR/account is required. Provide a copyable command, compactly.

**Hosted zero state**

Explain that assigned/requested reviews appear here and surface the real configured creation/provider path when available.

**Acceptance**

- zero states contain a next action;
- normal returning-user empty filters stay compact;
- no marketing hero treatment.

### RUX-007 — Next-action hierarchy

Use existing target/frontier/readiness state to make the next reviewer action obvious:

- review next target;
- address changed-since-review target;
- handle open request/comment;
- publish/send feedback;
- finish review.

**Acceptance**

- exactly one primary next action at a time;
- experts can still jump directly through outline/search;
- no opaque risk score or AI-generated authority replaces human judgment.

### RUX-008 — Explain semantic judgment state

Replace engine vocabulary in visible copy with causal reviewer language, e.g.:

- `Changed since you reviewed — your earlier judgment no longer covers this target.`
- `Still reviewed — this target did not change in the new revision.`
- `Review state uncertain — LemonCrow could not safely carry your prior judgment forward.`

**Acceptance**

- explanations are concise and tied to the affected target/revision;
- raw technical state remains available to diagnostics/tests, not primary copy.

### RUX-009 — Local workspace state / refresh

Expose workspace-specific actions only when advertised:

- current branch/range;
- workspace changed/new revision available;
- refresh/capture current workspace;
- open source/worktree where supported.

Do not make Local mimic remote-provider review.

### RUX-010 — Hosted collaboration strip

Promote only actionable collaboration facts:

- who requested the review;
- whether the current user owes judgment;
- author addressing requests / waiting state;
- revision arrived since review;
- scoped reviewer progress.

People/provider/policy sheets remain deeper surfaces.

### RUX-011 — Progressive-disclosure audit

Audit Source/Impact/Checks/Evidence/History/Provenance and specialized preview controls. The default surface should answer the review question before exposing configuration or provenance detail.

### RUX-012 — Component-boundary decomposition

`ReviewReader.tsx` and `ReviewStream.tsx` currently carry many product concerns. Extract boundaries only when they correspond to stable UX concepts, for example:

- `ReviewIdentityBar`;
- `ReviewAttentionStrip`;
- `ReviewNavigator`;
- `ReviewCanvas` + surface renderers;
- `ReviewJudgment`;
- `ReviewInspector`;
- local/hosted shell extensions.

No large rewrite. Preserve tests and behavior while reducing cross-concern coupling.

### RUX-013 — Responsive/accessibility qualification

Re-run and extend:

- keyboard navigation/focus recovery;
- screen-reader labels for mode/attention state;
- 1280px/narrow-laptop diff protection;
- mobile/narrow directory and hosted inbox behavior;
- reduced motion;
- modal/sheet focus traps.

### RUX-014 — Dogfood gate

Required before declaring this ledger complete:

1. local working-tree review from CLI to finish;
2. local revision refresh where prior judgment is preserved/invalidated;
3. hosted two-account requester/reviewer loop;
4. hosted new revision + changed-since-review loop;
5. provider-linked review when configured;
6. first-use zero state in both modes;
7. failure/degraded state with Review content still readable where possible.

Record screenshots/observations and update wording in this file rather than creating a separate UX TODO list.

## 5. Non-goals

This execution does **not** reopen:

- repository hosting;
- merge queue / branch protection;
- autonomous merge approval;
- generic issue/project management;
- a full IDE/editor;
- opaque AI risk/effort scores;
- new hosted production infrastructure already owned by the hosted readiness ledger.

## 6. Change log

### 2026-09-23

- Created canonical Local/Hosted Review product UX ledger.
- Started implementation from current `main` in isolated worktree `feat/review-product-ux`.
- **RUX-001 DONE:** added explicit `local | customer_hosted | managed_dedicated | managed_shared` deployment discovery plus Review product-capability discovery. First-party GCP declares `managed_dedicated`; the enterprise chart defaults to `customer_hosted`. Discovery remains tenant-free and does not infer topology from hostname/auth/org count.
- **RUX-002 DONE:** added a shared typed/cached `ReviewEnvironment` reader in the public frontend. Older-server fallbacks are conservative: capabilities are unavailable unless discovery advertises them; Local alone retains known local workspace behavior.
- **RUX-003 DONE:** the shared Reader identity strip now makes mode explicit without a banner: Local shows workspace/range context, Hosted shows deployment mode plus repository identity, and the mode indicator remains visible on mobile while secondary author/source/repository detail collapses.
- **RUX-004 DONE:** Local `/reviews` is action-first: visible rows project semantic progress, changed-since-review / needs-changes / ready-to-finish state, source and repository context, and collapse to Change + Action on narrow screens. First-use still teaches `$ lc review` without hosted concepts.
- **RUX-005 DONE:** Hosted inbox rows now prioritize the current user action over generic status/author columns, expose requester/author/scope/focus context when available, keep attention visually stronger, and collapse to Change + Action on narrow screens. Bucket navigation is horizontally scrollable rather than compressing labels.
- **RUX-006 DONE:** Local and Hosted have distinct compact first-use/zero-state guidance. Local teaches `$ lc review` and explicitly says no PR/account is required. Hosted `/v1/reviews` now returns authenticated creation metadata (`workspace_capture`, configured providers), so the empty state names the real workspace/agent or configured-provider path without inventing a browser-only create action.
- **RUX-008 DONE:** target state copy now explains preserved, invalidated, and uncertain judgments in reviewer language while retaining exact technical state for accessibility/tests.
- **RUX-007 DONE:** audited the existing single-primary-action state machine, retained its changed-work/comment/readiness ordering, and replaced engine-facing `target` wording in the primary workflow with `review area` language (`Review next area`, `Re-review changed area`). Full shared Reader regression suite remains green.
- **RUX-009 DONE:** Local Reader names its source (`Working tree`, `Staged changes`, or range), gates workspace probing/refresh through `workspace_refresh`, preserves the settled-source capture/compare flow, and offers `Copy workspace path` as the portable escape hatch. No fake `file://`/editor action is exposed because the current runtime has no supported cross-environment open-worktree contract.
- **RUX-010 DONE:** hosted review detail returns the authenticated `current_principal_id`; Reader collaboration state is derived from durable request + annotation turn ownership, showing `N left`, `N returned`, `Waiting on author`, or explicit attention without inferring who “you” are. Requester context remains available in the same compact header surface.
- **RUX-011 DONE:** Context now keeps review-work sections (`Impact`, `Comments`, `Checks`) primary while moving `Evidence`, `Author & provenance`, and `History` behind a keyboard-accessible `Details` disclosure. Selecting an expert section restores it as a proper tab so tab/panel semantics remain intact.
- **RUX-012 DONE:** extracted two stable header concepts without a visual rewrite: `ReviewIdentityBar` owns review ID/title/authorship/mode/source/repository/revision context, and `ReviewActionMenu` owns refresh/finish/copy/patch/order/theme/context/focus/feedback actions. `ReaderHeader` is now orchestration plus the primary review controls rather than owning every product concern.
- **RUX-013 DONE:** keyboard/focus and screen-reader contracts are covered by the Reader/header/Context suites; reduced-motion CSS remains present. Real Chromium qualification at 1280px and an exact emulated 390px viewport showed no document/toolbar horizontal overflow, kept the Local mode visible on mobile, hid secondary author/source detail as designed, kept the primary action inside the viewport, and collapsed the Local directory to Change + Action.
- **RUX-014 DONE:** dogfood gate completed. A real temporary Git repository was reviewed through `lc review`: revision 2 finished with both `alpha` and `beta` reviewed; revision 3 changed only `alpha`, producing exactly `1 changed_since_review` while preserving `beta` as reviewed. The new worktree bundle was loaded in real Chromium from an isolated Local server, including an exact 390px viewport and a real Local empty state (`No local reviews yet`, `$ lc review`, no PR/account required). The same degraded review remained readable while `astgrep_unavailable`, centrality, provenance ambiguity, and symbol-relation degradation were reported. A standalone enterprise HTTP dogfood used independent `user_a` and `user_rev` identities and observed `needs_my_review → waiting_on_author → returned_to_me → ready → changed_since_my_review`; the provider-configured design-partner acceptance story also passed on both backend variants, including provider ingestion/publication and rev-2 reconciliation.

## 7. Validation evidence — 2026-09-23

- Shared Review frontend: `115 passed` across Reader, identity/header, primary action, semantic target state, directory, Context progressive disclosure, command palette, and environment discovery; TypeScript check passed.
- Hosted Review frontend: `16 passed` across inbox/onboarding, participants, authenticated-current-user assignment, requester context, and durable turn-state projection; TypeScript check passed.
- Enterprise server focused suite: `77 passed` across deployment config/discovery, authenticated creation metadata, inbox/requester projection, reviewer outcome/current-principal behavior, collaboration state, and the configured-provider acceptance story.
- Local server + real local Review flow: `10 passed`, including a non-empty inbox progress projection.
- Real Local CLI dogfood: revision 2 finished `2/2 reviewed`; changing only `alpha` produced revision 3 with `alpha=changed_since_review` and `beta=reviewed` preserved.
- Real Chromium Local Reader: 1280px and exact 390px CSS viewports had `scrollWidth == innerWidth`; mode remained visible, secondary context collapsed on mobile, and the primary action stayed in bounds. Local `/reviews` also collapsed secondary columns at 390px.
- Real Chromium Local zero state showed `No local reviews yet`, copyable `$ lc review`, and `No PR or hosted account is required`, with no load error.
- Standalone two-account Hosted HTTP dogfood observed `needs_my_review → waiting_on_author → returned_to_me → ready → changed_since_my_review` using independent author/reviewer credentials.
- Configured-provider design-partner acceptance story passed on both backend variants, including provider ingestion, independent reviewers, new-revision reconciliation, explicit publication, and Inbox rediscovery.
- Changed Python modules compile cleanly.
- GCP Terraform formatting check passed.
- `git diff --check` passed.
## 8. Post-completion wide-diff polish — 2026-09-23

- Added a viewport-docked horizontal scrollbar for the active overflowing code diff. It mirrors Pierre's real inner `[data-code]` scroll position in both directions and disappears when the active diff fits horizontally, so a reviewer never has to travel to the bottom of a very long file merely to pan right or left.
- Added a persisted three-way diff-layout preference: **Auto** (default), **Split**, and **Unified**. Auto resolves from the actual available Review diff viewport, not raw window width: it uses Unified below 1000 px and Split when at least 1000 px is available. Explicit Split/Unified always overrides responsive resolution.
- Real Chromium evidence: with preference `auto`, an exact 390 px browser produced a 350 px Review viewport and resolved to Unified; a 1400 px browser produced an 1144 px Review viewport and resolved to Split.
- Real Pierre wide-diff evidence: a deliberately long changed line produced a 7236 px horizontal range. The dock moved the actual code scroller `0 → 240`, and scrolling the actual code element back to `125` moved the dock to `125`.
- Focused qualification: `122 passed` across `diffModel`, `ReviewStream`, `ReaderHeader`, and `ReviewReader`; TypeScript check and production frontend build passed; `git diff --check` passed.
