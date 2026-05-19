# W6 — Team tier: workspace, RBAC, attribution

> Parent: [`index.md`](index.md). Driving spec: [`day90/12-team-tier.md`](../../../specs/day90/12-team-tier.md).
> Depends on: **W1** (single-user rollback), **W4** (sync transport), **W5** (typed lesson scope).

## Goal

Turn the single-user wedge into a **multi-user product**: a team workspace
backed by W4's sync, with RBAC on memory + lessons, per-user cost
attribution from the existing run ledger, an SSO entry point, and a
team-scoped audit log that is the multi-user variant of W0/W1.

W6 is the first milestone that touches the `atelier-cloud` service for
real. Up to here, Atelier Cloud is a feature-flagged client. W6 turns the
flag on for paying teams.

## Why now

- W6 is where the recurring revenue lives. Solo Pro funds the runway; Team
  funds the business.
- Everything before W6 is single-user; everything after W6 is a multiplier
  on that revenue (W7 audit export, future approval workflows, future
  budget alerts).
- All foundations are now in place: audit log (W0), rollback (W1),
  encrypted sync (W4), typed scoped lessons (W5). W6 does the multi-user
  reshaping — not the substrate work.

## Where the code lives

```
src/atelier/core/capabilities/team/
  __init__.py                NEW package
  workspace.py               NEW — workspace lifecycle, invites
  rbac.py                    NEW — role checks (admin / member / viewer)
  attribution.py             NEW — per-user cost rollup from run_ledger
  sso/
    google.py                NEW — Google Workspace OIDC
    saml.py                  NEW (stub) — generic SAML; bare minimum in v1
src/atelier/core/capabilities/sync/
  backends/cloud.py          EXTEND — multi-user namespace, role enforcement
src/atelier/core/capabilities/cross_vendor_memory/
  scope.py                   NEW — fact visibility filter (own / shared / team)
src/atelier/core/capabilities/lesson_promotion/
  store.py                   EXTEND — honour `team` scope from W5
src/atelier/gateway/adapters/cli.py
                             EXTEND — `team init/invite/join/usage/role`
src/atelier/core/service/api.py
                             EXTEND — `/team/*` endpoints
tests/core/capabilities/team/
  test_workspace_lifecycle.py
  test_rbac_membership_enforcement.py
  test_attribution_per_user_rollup.py
  test_shared_memory_visibility.py
  test_sso_google_oidc_roundtrip.py
```

## The five pillars

W6 ships the five pillars spec 12 lists, each backed by an existing
primitive from W0–W5:

| Pillar                       | Substrate it builds on                                          |
|------------------------------|-----------------------------------------------------------------|
| **Cost attribution**         | `run_ledger.py` + `cost_tracker.py` already key per session     |
| **Shared memory**            | Local memory store (with new `scope` field) synced via W4       |
| **SSO** (Google, SAML stub)  | Google OIDC for v1; SAML stub for enterprise pipeline           |
| **RBAC on memory + lessons** | New `rbac.py` consulted by memory store + lesson store (W5)     |
| **Team-scoped audit log**    | W0/W1 logs replicated team-wide through W4                      |

## Workspace model

- One **workspace** per team. A workspace owns an `account_id` (W4's bucket
  prefix becomes `account_id/team_id/...`).
- Members have one of three roles: **admin**, **member**, **viewer**.
- An admin can: invite/remove members, see all memory in the workspace,
  approve team-scoped lessons, export audit (W7).
- A member can: share local facts to the workspace, see shared facts + own
  private facts, run routing/sync.
- A viewer can: see shared facts + workspace cost view; cannot write.

`admin` requires SSO (Google in v1) — invite codes alone are not enough to
hold admin. Members and viewers can join with invite codes; SSO is
optional.

## Memory scope

A memory fact in a team workspace carries one of:

- `private` — only the owning user sees it (default for a member's local
  upserts).
- `shared` — everyone in the workspace sees it (requires explicit
  `atelier memory share <fact-id>` or admin upsert).

Cross-vendor memory (Claude/Codex/Gemini) is **always treated as private
to the machine** — we do not auto-share what a member's local Claude
auto-memory captured. Sharing requires an explicit promotion to a workspace
fact.

## Attribution

`attribution.py` consumes `run_ledger.py` data the system already records,
keyed by `user_id` (new field) instead of just `machine_id`. The mapping
machine→user is set at `team join` and immutable thereafter without admin
action.

```
$ atelier team usage --since 30d
  alice@acme.com   $452.20   62 sessions
  bob@acme.com     $381.10   58 sessions
  total:           $833.30  120 sessions
```

## SSO

- v1: **Google Workspace OIDC** end-to-end. Standard authorization-code
  flow; tokens cached in the OS keyring like W4's passphrase.
- v1: generic **SAML stub** that returns "configured but not yet
  certified" — present so enterprise prospects see the lane.
- Okta lives behind the SAML stub; explicit Okta integration is W6+.

## User-visible CLI

```
# Admin bootstrap
$ atelier team init --name "Acme Engineering"
$ atelier team invite alice@acme.com bob@acme.com --role member
$ atelier team role alice@acme.com admin

# Member
$ atelier team join <invite-code>
$ atelier memory share <fact-id>          # promote local fact to team
$ atelier memory list --shared            # team-shared facts only

# Admin observability
$ atelier team usage --since 30d
$ atelier team audit --since 30d          # multi-user variant of W1's audit
```

## Validation

- `test_workspace_lifecycle` — init → invite → join → role change → leave
  round trip.
- `test_rbac_membership_enforcement` — viewer can't write shared facts;
  member can't change roles; admin can.
- `test_shared_memory_visibility` — private facts invisible to other
  members; shared facts visible to all roles incl. viewer.
- `test_attribution_per_user_rollup` — fixture sessions across three users
  produce the documented totals.
- `test_team_scoped_lesson_isolation` — W5 lesson with `scope=team` only
  applies for that team's members.
- `test_sso_google_oidc_roundtrip` — mocked OIDC flow yields a valid
  session.
- `test_admin_required_for_team_audit_export` — non-admin attempt returns
  403; admin succeeds.

## Exit criteria

- Workspace lifecycle works against the Atelier Cloud staging service.
- RBAC enforced on every write path; covered by negative tests.
- Per-user attribution view matches a hand-computed total on a fixture.
- Google OIDC works end-to-end; SAML stub responds with the documented
  "configured but not certified" status.
- W5 team-scoped lessons work; W1 rollback works against shared facts
  with admin permission.
- The Atelier Cloud feature flag (set in W4) is on by default once a team
  workspace exists on this machine.
- Trace recorded via `mcp__atelier__record`.

## Out of scope (this milestone)

- **Approval workflows on memory writes.** Future.
- **Team budgets and alerts.** Future.
- **Team-specific routing policies beyond W5 lessons.** Future; the W5
  store is enough for "Gemini Flash for reads".
- **Okta-specific integration.** SAML stub covers the lane; Okta cert is
  W6+.
- **EU residency.** Single region in v1.
- **Audit export.** W7.

## Open questions

1. **Pricing.** Spec 12 floats $30/user; product call, not engineering.
   Plan unblocks any number; pricing is a separate decision.
2. **Invite code lifetime.** Default: **7 days, one-time use**. Admin can
   re-mint.
3. **Memory share defaults.** Should `atelier memory upsert --shared` be a
   one-step flag, or always two-step (upsert → share)? Default:
   **two-step**, to keep accidental shares rare.
4. **Machine ↔ user binding immutability.** Default: **immutable without
   admin reset**, to keep attribution clean.
5. **Cross-vendor memory ever shared?** Default: **no, never auto**; an
   explicit `memory share --from-vendor` is a future command, not v1.
