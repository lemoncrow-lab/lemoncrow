# Live Review Runtime — instant author ↔ reviewer loop

Status: implementation tracking
Branch: `feat/live-review-runtime`

## Product invariant

`lcr` is an **open operation**, not a build operation. While an agent is authoring, LemonCrow continuously maintains the reviewable change and its review-relevant context. Opening Review must not rediscover work LemonCrow already observed.

The same durable `r/<id>` follows the authoring session across terminal, agent, and browser. Immutable `rr/<id>` records remain checkpoints of exact source bytes; they are not a prerequisite for opening the Reader.

## Experience

1. An agent session starts. LemonCrow establishes/reuses a live Review identity for the repository/change.
2. Reads, edits, commands, tests, explicit plans/rationale/decisions, evidence and model-visible outputs flow into a bounded review journal. Raw hidden chain-of-thought is never captured.
3. Each edit updates the persistent workspace View and invalidates only affected review projections.
4. `lcr` resolves the live Review and opens `r/<id>` immediately. The Reader renders the latest live diff first.
5. Impact, provenance, ranking, previews, evidence projection and AI review annotations enrich in place and never block source rendering.
6. Before a human comment/mark/outcome is accepted, the live source generation is checkpointed to an immutable `rr/<id>` and the action is anchored to those exact bytes.
7. Submitting feedback publishes it to the exact author-session inbox. A running agent consumes it on its next safe turn; an idle resumable agent is awakened through the existing delivery adapter.
8. Agent acknowledgement, fix progress, edits, verification and responses stream/poll back into the same Review. Returning to the terminal resumes the same author session and LemonCrow memory/context.

## What already exists

- Stable Review sessions and immutable revisions, including Review sessions with zero revisions.
- Exact content-derived ReviewTargets and safe cross-revision annotation re-anchoring.
- `attach_changed_symbols`, `attach_impact`, `enrich_revision_projection`, and identity-safe `update_revision_projection` for progressive enrichment.
- Per-session RunLedger events for tool calls, commands, tests and file edits, including exact edit diffs plus host/session/model/HEAD attribution.
- Author rationale and agent evidence sidecars bound to exact file/source identities.
- Human-feedback delivery to exact Claude/Codex/OpenCode/Copilot author sessions and `review_feedback_addressed` acknowledgement.
- Reader support for preparation stages and source-first progressive loading.
- Long-lived thin-client `RemoteSession` Views that already update incrementally as source changes.

## Current latency defect

The gateway `lc review` path ignores the live authoring state. It rebuilds a packet, opens a fresh remote session, freezes a base snapshot, freezes a current snapshot, publishes a revision, reloads durable state, and only then opens the browser. In the observed run the semantic passes cost ~0.3 s while snapshot/publish cost ~7.5 s. The critical fix is reuse, not merely moving impact analysis to a thread.

## Runtime design

### 1. LiveReviewHandle

One handle per `(repo_id, source_ref, range_mode)` with optional exact author `(host, session_id)`. It contains:

- stable Review id / `r/` ref;
- persistent authoring View id + current View revision;
- base source identity;
- current source fingerprint / generation;
- exact author session identity when known;
- enrichment generation and status;
- last immutable checkpoint revision;
- bounded review-journal cursor.

The handle is server/runtime state, not a second Review database. Durable human state stays in `ReviewStore`.

### 2. Live source generation

The existing long-lived `RemoteSession` View is the mutable source generation. LemonCrow edit hooks already know which paths changed and push/reindex them. Review consumes that View directly instead of materializing another full current-tree snapshot.

The base side is stable for a working-tree Review and is prepared once per HEAD/base change, then reused. A HEAD/base transition invalidates the base generation and starts a new Review source lineage only when the normal Review reopen identity says it should.

### 3. Progressive projection

Foreground, in order:

1. exact diff + changed blobs;
2. stable file/hunk/symbol target identity;
3. Reader source payload.

Background:

- impact/callers/contracts;
- provenance correlation;
- ranking/reasons;
- author rationale/evidence projection;
- AI review annotations;
- previews/surfaces and expensive verification.

`enrich_revision_projection` is used only when target identity is unchanged. Enrichment may improve metadata; it may never rewrite visible code identity.

### 4. Human-action checkpoint gate

Reading may target a live generation. Any mutation that carries human judgment (`comment`, `mark`, `outcome`, feedback delivery) calls `ensure_checkpoint(generation)` first. The checkpoint is idempotent by source/tree fingerprint. The UI may briefly show “pinning this version” for a race, but normal authoring should keep a checkpoint warm enough that this is effectively immediate.

### 5. Review journal

Capture only observable review-relevant information:

- user/agent-visible messages relevant to changed code;
- tool call + bounded result summaries;
- edit diffs and provenance;
- commands/tests/build/lint outcomes;
- explicit plan, rationale, decisions, alternatives and uncertainty when stated;
- agent-created annotations and evidence;
- human discussion threads and author responses.

Do **not** persist private hidden chain-of-thought. Where a provider exposes only internal reasoning, store no substitute; derive a short explicit decision/rationale summary only from observable actions/output.

Every journal item carries session identity, timestamp and optional source/path/blob fingerprint so projection onto a revision is auditable.

### 6. Feedback inbox

Existing delivery wakes/resumes idle sessions. Add a session inbox for already-running authors:

- Review submit writes an idempotent feedback bundle + annotation ids to the exact session inbox;
- host hooks/MCP bridge check the inbox before the next model turn and inject one compact feedback message;
- agent acknowledgement transitions delivery to in-flight/addressed;
- edits and verification update Review without a separate refresh command;
- fallback resume adapters remain for inactive sessions.

### 7. Cross-surface continuity

Terminal, MCP agent, and Reader resolve the same `(review_id, author_session_id, repo_id)` binding. The terminal can show `Review r/... · 3 comments · agent working` and `lcr` merely opens it. Returning from the browser does not create a new session or lose LemonCrow recall/memory.

## Implementation slices

### P0 — remove `lcr` from the build critical path

- [ ] Introduce `LiveReviewHandle`/binding owned by the long-lived runtime.
- [ ] Reuse the authoring `RemoteSession` View instead of freezing a second current snapshot.
- [ ] Prepare/reuse base View outside `lcr`.
- [ ] Create/reuse stable Review id before expensive enrichment.
- [ ] Make `lcr` resolve/pair/open first; checkpoint/enrichment continues independently.
- [ ] Preserve terminal-only `lc review --no-open` deterministic behavior.

Acceptance: with a warm plugin/runtime and already-observed edits, `lcr` performs no full worktree freeze and browser navigation starts before impact/provenance/ranking.

### P1 — continuous review preparation

- [ ] Feed successful LemonCrow edit events into live Review invalidation.
- [ ] Feed native host PostToolUse edit events into the same coordinator.
- [ ] Debounce/coalesce multi-edit bursts by source generation.
- [ ] Add filesystem fallback for out-of-band edits only.
- [ ] Incrementally derive diff/targets and background enrichment.

Acceptance: after an edit settles, opening Review requires no repository-wide rebuild.

### P2 — review journal / AI context

- [ ] Normalize observable RunLedger + host-session events into bounded review journal records.
- [ ] Project explicit rationale/decisions/plans/evidence to exact changed code where identity permits.
- [ ] Surface journal context in Reader without turning every tool event into reviewer noise.
- [ ] Deduplicate/relevance-rank AI annotations and preserve source/confidence.

### P3 — live feedback handoff

- [ ] Add exact-session feedback inbox for running agents.
- [ ] Consume inbox in Claude/Codex/OpenCode/Copilot/LemonCode bridges.
- [ ] Surface queued → delivered → agent working → addressed/fixed → needs rereview states live.
- [ ] Keep existing resume adapters as idle-session fallback.

### P4 — continuity and hardening

- [ ] Terminal status/resume commands use the same Review/session binding.
- [ ] Browser live update transport (SSE where useful, bounded polling fallback).
- [ ] Crash/restart recovery from durable handle/journal cursors.
- [ ] Hosted parity: tenant isolation, quotas, retention and no local-path assumptions.
- [ ] Benchmarks for warm `lcr` navigation, first diff, checkpoint, feedback-to-agent latency.

## Correctness constraints

- Human judgments always bind to immutable exact bytes.
- Analysis enrichment cannot change ReviewTarget identity already visible to a human.
- A stale live generation can never receive a judgment intended for a newer one.
- Review id continuity follows existing Review reopen identity, not host-session guesses.
- Exact author-session attribution is preferred; uncertain correlation remains explicitly uncertain.
- No hidden chain-of-thought persistence.
- Local and hosted modes share the same Review semantics even when source transport differs.
