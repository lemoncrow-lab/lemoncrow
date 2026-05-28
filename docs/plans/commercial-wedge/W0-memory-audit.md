# W0 — Memory audit log: append-only ground truth

> Parent: [`index.md`](index.md). Driving spec: [`day30/08-memory-audit-viewer.md`](../../../specs/day30/08-memory-audit-viewer.md).
> Prerequisite for W1 (rollback), W6 (team audit), W7 (audit export).

## Goal

Capture every change to every vendor memory file (Claude, Codex, Gemini) into
an **append-only** local audit log, plus periodic snapshots, **without yet
shipping rollback writes**. The user-visible surface for this milestone is
read-only: `atelier memory diff` over a time window.

The existing `cross_vendor_memory/registry.py` already knows how to *read*
vendor memory files. W0 adds the *change-detection* layer on top of it.

## Why first

- Every other commercial conversation — rollback, trust, team audit, export —
  routes through "do you have a tamper-evident record of what changed?"
- Cheap to ship: no writes to vendor files yet, no cloud dependency.
- Forces us to lock the audit record shape **before** any consumer hard-codes
  it. W1, W6, W7 all serialise this shape.
- Lets us start instrumenting the diff-between-runs metric (the input to the
  Pro-tier counterfactual story in W2) without committing to writes.

## Where the code lives

```
src/atelier/core/capabilities/cross_vendor_memory/
  registry.py                EXISTS — reused, no behavioural change
  audit_log.py               NEW — append-only JSONL writer + reader
  snapshotter.py             NEW — hourly-stale diff job; invoked from CLI hook
  models.py                  NEW (or extend existing) — AuditEvent dataclass
src/atelier/gateway/adapters/cli.py
                             EXTEND — add `memory diff` (no rollback yet)
tests/core/capabilities/cross_vendor_memory/
  test_audit_log.py          NEW
  test_snapshotter.py        NEW
```

Audit log files: `~/.atelier/memory_audit/<vendor>.jsonl`.
Last-snapshot bookkeeping: `~/.atelier/memory_audit/_state.json`.

## Audit record shape (frozen by this milestone)

```json
{"v": 1,
 "at": "2026-05-18T14:02:11Z",
 "vendor": "claude" | "codex" | "gemini",
 "event": "added" | "removed" | "changed",
 "fact_id": "claude-a3b8",
 "source_file": "/abs/path/to/MEMORY.md",
 "source_line": 14,
 "content": "...",
 "previous_content": "..." | null,
 "actor": "vendor-auto" | "atelier-user" | "atelier-rollback",
 "machine_id": "studio-mbp"}
```

`fact_id` is content-hashed from `(vendor, normalised_content)` so the same
fact has the same id across machines (input to W4/W6 sync + team views).

## Snapshotter behaviour

- Triggered as a lightweight hook inside `atelier` command invocations: if
  `_state.json` says the last snapshot was >1h ago **and** no snapshot is in
  flight, run one. No system daemon, no cron.
- Reads each vendor's memory via `registry.py`, diffs against the previous
  snapshot, appends `added` / `removed` / `changed` events.
- Snapshot itself is **not** the audit log — the audit log is the
  append-only stream; the snapshot is a stored side table used as the diff
  baseline. Snapshots are overwritten; audit events are not.

## User-visible CLI (read-only this milestone)

```
$ atelier memory diff --since 24h [--vendor claude|codex|gemini]
+ [claude-a3b8] "Pankaj prefers explicit type hints"
- [codex-c2e1] "Always run pytest before committing"
~ [gemini-f8d2] "Email: pankaj4u4m@gmail.com"
```

No `rollback`, no `why` yet — those land in W1.

## Validation

- `test_audit_log_append_only` — opening the log file for write must use
  append mode; a unit test attempts to rewrite a prior line and asserts the
  writer rejects it (or the verifier flags it).
- `test_audit_event_schema_v1_frozen` — schema golden test: every field name
  and type pinned. Future changes bump `v` and add migration tests.
- `test_snapshotter_skips_when_recent` — when `_state.json` shows a snapshot
  <1h ago, snapshotter is a no-op.
- `test_diff_reports_added_removed_changed_correctly` — fixture vendor file
  pair (before/after) produces exactly the expected event set.
- `test_fact_id_stable_across_machines` — same `(vendor, content)` on two
  fake machine IDs yields the same `fact_id`.
- `test_memory_diff_cli_window_filtering` — `--since 24h` only returns
  events in window; vendor filter narrows correctly.

## Exit criteria

- Audit JSONL exists for every configured vendor and grows on changes.
- `atelier memory diff` reports added/removed/changed in a window.
- Snapshotter runs idempotently from the CLI hook with no daemon.
- Schema v1 has a golden test guarding the field set.
- No vendor files are written; the system is strictly read + log.
- Trace recorded via `mcp__atelier__record` referencing this milestone.

## Out of scope (this milestone)

- Rollback writes to vendor files — W1.
- Provenance ("why" command) — W1.
- Cross-machine reconciliation of audit logs — W4 ships the transport; the
  audit log is plain JSONL so it rides W4 unchanged.
- Tamper signatures — W7. (Append-only is enforced by the writer in W0; a
  signed transcript is the W7 export.)

## Open questions

1. **Snapshot cadence.** 1h default — should it be configurable per vendor?
   Default: **no, single global cadence**; revisit if a vendor file is
   churned more than once an hour by its host.
2. **Where do we store the previous-snapshot baseline?** SQLite table inside
   the existing `sqlite_memory_store.py` DB, or a flat JSON sibling? Default:
   **flat JSON sibling** — easier to inspect, easier to delete and rebuild.
3. **Vendor file path discovery.** Hardcoded today in `registry.py`. Should
   the snapshotter accept overrides for unusual installs? Default: **yes via
   `~/.atelier/cross_vendor_memory.yaml`**, no env-var sprawl.
