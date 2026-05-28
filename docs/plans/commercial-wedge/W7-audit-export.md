# W7 — Audit export + governance policy enforcement

> Parent: [`index.md`](index.md). Driving spec: [`day90/12-team-tier.md`](../../../specs/day90/12-team-tier.md) + [`day30/08-memory-audit-viewer.md`](../../../specs/day30/08-memory-audit-viewer.md).
> Depends on: **W1** (audit log + rollback events), **W5** (typed lesson decisions), **W6** (team scope).
> Closes the commercial-wedge plan.

## Goal

Give a team admin one command that produces a **signed, reproducible,
human-readable** export of everything the system decided and changed over
a window — memory events, route decisions, lesson bindings, rollbacks,
sessions, costs — and an enforcement layer for the retention / governance
policy the team configures.

This is the artifact a security review asks for, the artifact a SOC 2
auditor will accept once we go through that process, and the thing that
makes "auditability is the moat" a concrete deliverable instead of a slide.

## Why last

- W7 is the union of everything before it. Shipping it earlier would
  leave half the bundle empty.
- It's the closing capability for the wedge plan; once W7 ships, future
  governance work (approval workflows, budget alerts, policy DSL) gets a
  fresh plan.
- It is the trust artifact that lets a team upgrade beyond Team tier into
  whatever Enterprise tier we eventually offer.

## Where the code lives

```
src/atelier/core/capabilities/audit_export/
  __init__.py                NEW package
  bundler.py                 NEW — gathers W0/W1/W3/W5/W6 events into one stream
  signer.py                  NEW — detached signature over the bundle
  manifest.py                NEW — bundle manifest with schema + checksums
src/atelier/core/capabilities/governance/
  policy.py                  NEW — retention + redaction policy DSL
  enforcer.py                NEW — applies policy to all writers (audit, memory, sync)
src/atelier/gateway/adapters/cli.py
                             EXTEND — `audit export`, `audit verify`, `governance show/apply`
src/atelier/core/service/api.py
                             EXTEND — `/audit/export`, `/governance/policy`
tests/core/capabilities/audit_export/
  test_bundle_round_trip_and_verify.py
  test_signature_detects_tampering.py
  test_policy_retention_drops_expired_events.py
  test_policy_redaction_applies_pre_export.py
```

## Bundle shape

A bundle is a directory (or `.tar.zst`) with this fixed layout:

```
atelier-audit-<team_id>-<from>-<to>/
  manifest.json            schema version, time range, checksums, signer key id
  memory_events.jsonl      W0 + W1 (added/removed/changed/rollback) for the team
  route_decisions.jsonl    W3 advisor recommendations + outcome
  lessons.jsonl            W5 typed lessons applied in window
  sessions.jsonl           session reports + per-user attribution (W6)
  rollbacks.jsonl          W1 events with actor=atelier-rollback only
  README.txt               human-readable summary; one paragraph
atelier-audit-<...>.sig    detached signature file
```

Everything is JSONL so it's grep-able without tooling. The detached
signature is a sibling file so the bundle stays plain text.

## Signing

- Per-team signing key, generated at `team init` (W6) and held in the
  workspace's keyring.
- Detached signature over `manifest.json` (which itself contains a
  hash-tree of every file in the bundle).
- `atelier audit verify <bundle>` re-walks the hash tree and validates the
  signature. Tampering at any level fails verification with a clear
  pointer to the offending file.

## Governance policy DSL

Small, opinionated YAML — not a Turing-complete rule engine.

```yaml
# ~/.atelier/governance.yaml (team-scoped, synced via W4)
retention:
  memory_audit_days: 365
  session_reports_days: 365
  rollback_events_days: 1825      # 5 years, never auto-deleted under floor
  backups_days: 90                # ~/.atelier/memory_backups (W1)
redaction:
  patterns:
    - kind: regex
      value: "(?i)secret|token|api[_-]?key"
      action: redact-content
exports:
  default_format: tar.zst
  always_signed: true
```

`enforcer.py` is invoked:
- Before any export (redaction).
- On a scheduled compaction step (retention) — runs from the same CLI hook
  W0 already uses for snapshots; no daemon.
- Inside the W1 backup writer (`backups_days`).

A retention sweep **never deletes rollback events** below the floor; the
floor is the legal-minimum window. Exceeding the floor requires a
`--force-purge` flag.

## User-visible CLI

```
# Export
$ atelier audit export --since 90d --out /tmp/acme-audit/
✓ Wrote 12,318 events into atelier-audit-acme-2026-02-18-2026-05-18/
✓ Signed with team key fpr 5C8B…1A04
✓ Verify with: atelier audit verify /tmp/acme-audit/atelier-audit-acme-...

# Verify
$ atelier audit verify /tmp/acme-audit/atelier-audit-acme-...
✓ Signature valid (team key fpr 5C8B…1A04)
✓ All 12,318 events hash-matched the manifest
✓ Bundle integrity: OK

# Governance
$ atelier governance show
$ atelier governance apply ./governance.yaml      # validates + activates
```

## Validation

- `test_bundle_round_trip_and_verify` — generate → verify → tamper one
  byte → verify fails with the specific file name.
- `test_signature_detects_tampering` — modify a JSONL line, manifest
  checksum changes, verify fails.
- `test_policy_retention_drops_expired_events` — events past
  `memory_audit_days` are not exported (and, on compaction, are GC'd
  unless they're rollback events).
- `test_policy_retention_protects_rollback_floor` — rollback events older
  than `memory_audit_days` but younger than `rollback_events_days` are
  retained.
- `test_policy_redaction_applies_pre_export` — patterns match in raw log;
  output bundle shows `[redacted: rule-id]`.
- `test_export_requires_admin_role` — non-admin call returns 403.
- `test_governance_apply_validates_yaml` — malformed YAML refused, no
  partial activation.

## Exit criteria

- `audit export` produces a deterministic bundle for a given time window
  and team.
- `audit verify` round-trips and detects single-byte tampering.
- Retention compaction runs from the existing CLI hook; rollback floor is
  honoured; redaction applies before export.
- Admin-only enforced on export and on governance apply.
- Bundle format is documented (schema + sample) under `docs/specs/` or
  `docs/architecture/`.
- Trace recorded via `mcp__atelier__record`.
- The wedge plan's `Status` is updated to **Shipped** in `index.md`.

## Out of scope (this milestone)

- **Live tamper-evident log** (Merkle / transparency log) — overkill for
  v1; the per-export signature is enough for SOC 2 and security review.
- **Per-user export limits / approval flows.** Future.
- **Web UI for audit browsing.** Lives in the day30/10 dashboard spec,
  not here.
- **External SIEM push** (Splunk / Datadog / etc). Future plan; bundle
  format is grep-friendly precisely so SIEM ingest is a thin wrapper.

## Open questions

1. **Signing key custody.** Workspace keyring on the admin's machine — but
   what if the admin leaves? Default: **admin handover rotates the key,
   old signatures stay verifiable with the published historical key**.
2. **Bundle format on disk.** Directory vs `.tar.zst`. Default: **both
   supported; `.tar.zst` is default for export, directory for verify
   sources**.
3. **Redaction granularity.** Field-level vs whole-event drop. Default:
   **content-level redact-in-place** with `[redacted: rule-id]`; whole
   events are never silently dropped.
4. **What ends the wedge plan?** Default: when W7 ships and the bundle is
   verified end-to-end against the Atelier Cloud staging service for a
   real (test) team, the wedge plan is marked Shipped. Further work
   (write-back, federated, leaderboard) gets fresh plans.
