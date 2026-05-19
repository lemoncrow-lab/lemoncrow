# Commercial wedge — sync, vendor rollback, learning loop, team tier

> Status: **Shipped** — all W0-W7 completed 2026-05-19.
> Owner: unassigned.
> Coordinates existing specs: 06 (sync), 07 (counterfactual), 08 (memory audit),
> 09 (cross-vendor routing), 11 (federated learning), 12 (team tier).
> See also: [`../../decisions/`](../../../decisions/) for ADRs as they land.

## Problem

Atelier already has the bones of a strong **local** runtime intelligence layer
for a solo developer: real session ledgers, cost reports, outcome capture,
cross-vendor memory **inspection**, and versioned editable memory. What the
product narrative promises but the codebase does not yet ship is the
**commercial wedge**:

1. Cross-machine portability of state (drives Pro tier).
2. Cross-vendor memory **rollback and audit**, not just read-only inspection
   (the trust signal teams buy).
3. A closed adaptive learning loop — cross-tool lessons that automatically
   reshape future routing/memory decisions (the moat).
4. Counterfactual replay surfaced to users per session, not just heuristic
   weekly opportunities (the viral feature).
5. Team governance — shared memory, RBAC, SSO, attribution, audit export
   (the revenue lever).

This plan turns an honest gap analysis into a sequenced execution plan against
the day30 / day90 specs that already exist. The specs describe **what** to
build; this plan declares **the order, the dependencies, and the validation
gates** across them, and it pins each promise to a concrete code path so we
stop conflating what we have with what we still owe.

## What is already in place — reuse, do not rebuild

| Capability                              | Where it lives                                                                                                                                                                                                  |
|-----------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Run ledger / session evidence           | `src/atelier/infra/runtime/run_ledger.py`, `src/atelier/core/foundation/store.py`, `src/atelier/infra/runtime/session_report.py`                                                                                 |
| CLI + API exposure of sessions          | `src/atelier/gateway/adapters/cli.py` (`session …`), `src/atelier/core/service/api.py` (session routes)                                                                                                         |
| Per-session cost + weekly insights      | `src/atelier/infra/runtime/cost_tracker.py`, `src/atelier/infra/runtime/insights.py`, CLI under `cost …`                                                                                                         |
| Outcome capture + scoring               | `src/atelier/infra/runtime/outcome_capture.py`, MCP hooks in `src/atelier/gateway/adapters/mcp_server.py`, inspection commands in `cli.py`                                                                       |
| Cross-vendor memory **reading**         | `src/atelier/core/capabilities/cross_vendor_memory/registry.py`, CLI `memory list/diff (read-only)`, API routes in `service/api.py`                                                                              |
| Local editable memory + version history | `src/atelier/gateway/adapters/cli.py` (`memory upsert/get/list/archive/recall`), `src/atelier/infra/storage/sqlite_memory_store.py`                                                                              |
| Lesson promotion (human-reviewed)       | `src/atelier/core/capabilities/lesson_promotion/capability.py`, CLI `lesson review`                                                                                                                              |
| Weekly governance report (solo)         | `src/atelier/core/capabilities/reporting/weekly_report.py`                                                                                                                                                       |
| Usage / metrics upstream sync           | `src/atelier/core/service/sync.py` — **note: this pushes telemetry, not user-facing state sync**                                                                                                                 |

These are the substrates the wedge milestones build on. Each milestone below
states which of the above it extends and what new module it adds.

## Gap inventory — what the narrative promises but the code does not ship

| Promise                                                       | Reality today                                                                                                       | Spec    |
|---------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------|---------|
| `atelier sync up/down` — encrypted cross-machine state sync   | Shipped via `core/capabilities/sync/` plus CLI/API sync surfaces.                                                   | 06      |
| `atelier memory diff / rollback / why` against vendor files   | Shipped with append-only audit log, provenance, and rollback flows.                                                 | 08      |
| Per-session counterfactual surfaced to the user               | Shipped through the session counterfactual/reporting path.                                                          | 07      |
| Live cross-vendor routing inside a session                    | Shipped through the route CLI/MCP surfaces and advisory runtime capture.                                            | 09      |
| Closed learning loop (lessons → routing/memory behaviour)     | Shipped for typed lessons (`route-preference`, `cost-cap`) with live binding and sync portability.                  | 11      |
| Team workspace, invites, RBAC, SSO, attribution               | Shipped with local workspace lifecycle, RBAC, audit, attribution, and Google-first bootstrap stub.                 | 12      |
| Cross-vendor memory **write-back** ("teach Claude what Codex knows") | Out of scope of 08; pencilled for 14 / future.                                                              | 14      |

## Milestones (sequenced)

Each milestone references its driving spec. Build order is justified by the
dependency graph below; do not reorder without updating it.

| ID  | File                                                          | Title                                              | Driving spec | Ships                                                                                                                                              |
|-----|---------------------------------------------------------------|----------------------------------------------------|--------------|----------------------------------------------------------------------------------------------------------------------------------------------------|
| W0  | [`W0-memory-audit.md`](W0-memory-audit.md)                    | Memory audit log — append-only ground truth        | 08           | `cross_vendor_memory/audit_log.py` + snapshotter that diffs vendor files on a >1h-stale hook. **No rollback yet.** Foundation for W1, W3 and W5.   |
| W1  | [`W1-memory-rollback.md`](W1-memory-rollback.md)              | Memory rollback + provenance (`why`)               | 08           | `cross_vendor_memory/rollback.py`, backup writer, `memory diff / rollback / why` CLI subcommands. Per-vendor format-safe writers.                  |
| W2  | [`W2-counterfactual.md`](W2-counterfactual.md)                | Per-session counterfactual command                 | 07           | `atelier session counterfactual <id>` — replays the session's tool/turn ledger against a price+capability table; uses existing cost_tracker data.  |
| W3  | [`W3-cross-vendor-routing.md`](W3-cross-vendor-routing.md)    | Cross-vendor live routing surface                  | 09           | `atelier route configure` + MCP route advisor that takes the within-vendor router and extends it across configured vendor keys.                    |
| W4  | [`W4-sync.md`](W4-sync.md)                                    | Cross-machine sync (encrypted, manual)             | 06           | `core/capabilities/sync/` package: encryption, backend interface, S3 backend, cloud backend, `atelier sync up/down/status`. **Renames the existing telemetry path to `core/service/usage_sync.py` so the user-facing `sync` namespace is unambiguous.** |
| W5  | [`W5-lesson-binding.md`](W5-lesson-binding.md)                | Adaptive lesson binding (closed loop, narrow)      | 11           | Promote lessons from inbox into a **typed lesson store** that the router and outcome capture consume on every decision; start with two lesson kinds only (route-preference, cost-cap).                                   |
| W6  | [`W6-team.md`](W6-team.md)                                    | Team tier — workspace, RBAC, attribution           | 12           | New `core/capabilities/team/` package: workspace creation, invite codes, role checks on memory + sync namespaces, per-user cost attribution view, SSO stub (Google first). Builds on W1 + W4. |
| W7  | [`W7-audit-export.md`](W7-audit-export.md)                    | Audit export + governance policy                   | 12 + 08      | `atelier audit export --since …` producing a signed JSONL bundle of decisions + memory events + sessions; per-team retention policy enforcer.       |

## Dependency graph

```
W0 (audit log)
 ├─► W1 (rollback + why)
 │    └─► W6 (team) — team rollback needs single-user rollback first
 │    └─► W7 (audit export) — audit export is the audit log + decisions, signed
 ├─► W2 (per-session counterfactual)  ← independent of W1
 ├─► W3 (cross-vendor routing)        ← independent; consumes outcome_capture
 ├─► W4 (cross-machine sync)          ← independent of W0–W3 but blocks W6
 │    └─► W6 (team workspace = shared sync namespace)
 ├─► W5 (lesson binding)              ← needs outcome_capture + lesson_promotion
 │    └─► W6 (team policies = team-scoped lessons)
 └─► W7 (audit export)                ← needs W1 and W5; W6 makes it team-scoped
```

Recommended build order: **W0 → W1 → W2 → W4 → W3 → W5 → W6 → W7**.

Rationale:
- W0/W1 first because every other commercial conversation ("can I trust this
  with my secrets?", "can my team roll back a bad memory write?") routes
  through audit.
- W2 next because it is a single-machine viral surface and unblocks marketing
  posts and pricing-page screenshots independent of cloud infra.
- W4 before W3 because `atelier sync` is the named Pro feature; live
  cross-vendor routing without portable state is a weaker pitch.
- W5 before W6 so team policies have a typed lesson store to attach to.
- W7 last because it is the union of everything above and would be hollow
  if shipped earlier.

## Validation gates (cross-milestone)

Before any milestone is marked completed:

- Acceptance criteria from the driving spec are satisfied and ticked in the
  spec file itself.
- New/changed rows added to `docs/agent-os/validation-matrix.md`.
- Unit tests under `tests/` for the milestone's slice.
- For W0/W1/W7: tamper test — manual edit of the audit log file must be
  detected by the verifier.
- For W2/W3: a counterfactual / route decision benchmark recorded under
  `tests/benchmarks/runtime/` comparing the new path against a baseline
  session.
- For W4: round-trip test against MinIO and against the staging atelier-cloud
  service, with network-failure injection mid-sync.
- A trace recorded via `mcp__atelier__record` referencing the milestone.

## Out of scope (deliberately)

- **Memory write-back across vendors** ("teach Claude what Codex knows") — out
  of scope of W1; will be reconsidered after W7 lands.
- **Real-time / push sync.** W4 ships manual `sync up/down`; an auto-sync
  timer is a follow-up after W4 stabilises.
- **Conflict resolution UI.** Last-write-wins for W4; a visual merge view
  lives behind W1's diff once sync is per-team in W6.
- **Counterfactual replay against an actual second vendor in-process.** W2
  uses price + capability tables. True replay against live APIs is a future
  spec because it doubles per-session API spend.
- **EU residency / on-prem cloud backend** — W6 keeps a single region; revisit
  once a paying team requests it.
- **Approval workflows on memory writes**, cost budgets/alerts, team-specific
  routing policies — all on the day90 backlog; do not pull forward.

## Open questions

1. **Sync backend default.** Spec 06 leaves this open. Do we launch W4 with
   Atelier Cloud as the recommended choice, or self-host S3 first to avoid
   running infra before revenue exists? Default proposal: **self-host S3
   first, Atelier Cloud once W6 has at least one paying team committed.**
2. **Lesson binding surface (W5).** Two lesson kinds at launch — which two?
   Default: `route-preference` (vendor/model per tool/phase) and `cost-cap`
   (per-session ceiling that flips the router down a tier). Anything else is
   W5+.
3. **Team identity (W6).** Magic-link reuse from spec 06, or skip straight to
   Google SSO? Default: magic link for invite; Google SSO required for admin
   role.
4. **Audit export signing (W7).** Detached signature (`.sig` next to bundle)
   or in-bundle? Default: detached, so the bundle stays grep-able JSONL.
5. **Where does this plan terminate?** Once W7 ships, the wedge plan is
   closed; further roadmap items (write-back, federated learning, public
   leaderboard) get fresh plans under `docs/plans/active/`.

## Status

- [x] Drafted — gap analysis pinned to code paths and specs
- [x] Reviewed
- [ ] Claimed (set an owner via `TaskUpdate`)
- [x] In progress
- [x] Shipped (when all of W0–W7 are marked complete in their spec files)
