# W1 — Memory rollback + provenance (`why`)

> Parent: [`index.md`](index.md). Driving spec: [`day30/08-memory-audit-viewer.md`](../../../specs/day30/08-memory-audit-viewer.md).
> Depends on: **W0** (audit log + snapshotter).

## Goal

Make every vendor memory change reversible from the Atelier CLI **without
risking the vendor file's format**, and add the provenance lookup the
"trust" pitch turns on.

W0 captured what changed; W1 lets the user undo it and ask "why is this fact
here?".

## Why next

- W0 by itself is read-only — useful but doesn't sell. Rollback is the
  feature that converts security-conscious teams.
- Has to land before any team-level rollback (W6) because team rollback is
  the multi-user variant of single-user rollback.
- Audit export (W7) needs to record `actor="atelier-rollback"` events; those
  only exist once W1 ships.

## Where the code lives

```
src/atelier/core/capabilities/cross_vendor_memory/
  rollback.py                NEW — applies inverse of an AuditEvent to source
  writers/                   NEW package — one safe writer per vendor
    claude_writer.py
    codex_writer.py
    gemini_writer.py
  provenance.py              NEW — `why <fact-id>` aggregator
src/atelier/gateway/adapters/cli.py
                             EXTEND — `memory rollback`, `memory why`
tests/core/capabilities/cross_vendor_memory/
  test_rollback_claude.py    NEW (per-vendor)
  test_rollback_codex.py     NEW
  test_rollback_gemini.py    NEW
  test_provenance.py         NEW
```

Backups: every write makes a copy at
`~/.atelier/memory_backups/<vendor>/<file-basename>-<iso8601>.bak`
**before** mutating the vendor file. Backups are not garbage-collected in W1;
W7 owns retention.

## Rollback rules

For each `AuditEvent` (W0):

| Event     | Inverse action                                                                            |
|-----------|--------------------------------------------------------------------------------------------|
| `added`   | Delete the line(s) the fact occupies in the vendor source file.                            |
| `removed` | Re-insert at `source_line`; if line drifted, append to the file with a `# restored` note.  |
| `changed` | Replace current content with `previous_content`; same drift fallback as `removed`.         |

Every rollback writes a new `AuditEvent` with `actor="atelier-rollback"`. So
"rollback a rollback" is just rolling back the new event — no special case.

## Per-vendor writers

Each writer owns the **format-safe** mutation for its vendor:

- `claude_writer.py` — Markdown bullet list; preserves heading/section
  ownership; never reorders unrelated lines.
- `codex_writer.py` — Memory file format (Markdown with optional
  front-matter); preserves front-matter ordering.
- `gemini_writer.py` — Vendor-specific structured file; round-trips through
  the existing parser in `registry.py` rather than line-editing.

A vendor file we can't safely mutate (unknown format) makes rollback refuse
with a clear error. **No silent best-effort writes.**

## User-visible CLI

```
$ atelier memory rollback --fact-id codex-c2e1
$ atelier memory rollback --since 24h          # confirm-prompt before write
$ atelier memory rollback --event-id <uuid>    # by exact audit event
$ atelier memory why <fact-id>                  # provenance, no writes
```

`why` aggregates:
- first-seen timestamp
- last-confirmation (latest snapshot that still saw the fact)
- confirmation count
- cross-vendor matches by substring similarity ≥ 0.8 (cheap shingle/Jaccard,
  not embeddings — embeddings live elsewhere)

## Validation

- `test_rollback_added_event_removes_line` — per-vendor; vendor file before
  matches the expected after.
- `test_rollback_removed_event_reinserts_at_original_line` — line still
  present in shape; surrounding lines untouched.
- `test_rollback_changed_event_reverts_content` — diff against backup is the
  inverse of the original change.
- `test_rollback_creates_backup_before_write` — file at backup path exists
  and matches pre-rollback hash.
- `test_rollback_emits_audit_event_with_actor_rollback` — new event chained
  into the log.
- `test_rollback_refuses_unknown_vendor_format` — no write occurs, exit code
  is non-zero, error message names the file.
- `test_why_returns_first_seen_and_confirmation_count` — fixture audit log
  with known event timeline.
- `test_why_cross_vendor_similarity_threshold` — facts above 0.8 are listed;
  below are not.

## Exit criteria

- All three vendor writers exist with per-vendor format tests.
- `memory rollback --fact-id` and `--since` work end-to-end on fixtures.
- Backups always precede writes; backup path is reported in CLI output.
- `memory why` returns the documented provenance fields.
- Rollback never produces a file the vendor reader (`registry.py`) cannot
  parse — verified by a round-trip test per vendor.
- Trace recorded via `mcp__atelier__record`.

## Out of scope (this milestone)

- **Write-back across vendors** ("teach Claude what Codex knows") — deferred
  past W7.
- **Auto-merge of conflicting facts** — `memory diff` shows conflicts; the
  user resolves with explicit rollback commands.
- **Team-scoped rollback** ("revert this for everyone") — W6.
- **Backup retention policy** — W7.

## Open questions

1. **Line-drift fallback.** Append with a `# restored: <iso8601>` comment vs
   refuse and ask the user. Default: **append with comment**; safer than
   refusing, traceable in the next audit event.
2. **Bulk rollback safety.** `--since 24h` could touch hundreds of facts.
   Hard cap on rollback batch size? Default: **50 events; bigger needs
   `--force`**.
3. **`why` similarity threshold.** 0.8 is a guess. Default: **0.8 for v1;
   record per-fact similarity in the output so we can tune later** without a
   schema change.
