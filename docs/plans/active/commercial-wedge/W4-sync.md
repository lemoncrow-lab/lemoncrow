# W4 — Cross-machine sync (encrypted, manual)

> Parent: [`index.md`](index.md). Driving spec: [`day30/06-cross-machine-sync.md`](../../../specs/day30/06-cross-machine-sync.md).
> Blocks: W6 (team workspace = shared sync namespace). Renames the existing telemetry path so the user-facing `sync` namespace is unambiguous.

## Goal

Ship the named Pro-tier feature: `atelier sync up` and `atelier sync down`
move encrypted Atelier state between a user's machines. Manual invocation
in v1 — no real-time push, no auto-timer, no UI.

State that syncs: memory facts (incl. W0 audit log), outcome capture,
session reports, route configuration (W3), counterfactual history (W2).
State that does **not**: active session events, vendor API keys, the user's
source repo.

## Why now

- It's the single feature most directly tied to a recurring price tag.
- W6 (team) is "shared sync namespace" — must follow W4.
- Per the index, W4 lands before W3 in the recommended order: the headline
  "sync your context across machines" is a stronger first paid feature than
  in-session routing.

## Naming hazard — resolve in this milestone

The current `src/atelier/core/service/sync.py` ships **telemetry** upstream,
not user-facing state. W4 takes the `sync` name. Rename the existing file
to `src/atelier/core/service/usage_sync.py` and update its call sites and
docs. This rename ships with W4, not earlier (no point churning the path
before there's a user-facing `sync` to disambiguate against).

## Where the code lives

```
src/atelier/core/capabilities/sync/
  __init__.py                NEW package
  sync_engine.py             NEW — orchestrator
  encryption.py              NEW — Argon2id key derivation + AEAD
  serializer.py              NEW — snapshot what gets synced (one place)
  merge.py                   NEW — last-write-wins per entity type
  backends/                  NEW package
    cloud.py                 NEW — Atelier Cloud client (later, behind flag)
    s3.py                    NEW — generic S3 / R2 / Backblaze
    ssh.py                   NEW — SCP-style self-host
  config.py                  NEW — ~/.atelier/sync.yaml reader
src/atelier/core/service/
  sync.py                    RENAME → usage_sync.py
src/atelier/gateway/adapters/cli.py
                             EXTEND — `sync init / up / down / status`
src/atelier/core/service/api.py
                             EXTEND — internal `/sync/*` endpoints (cloud backend only)
tests/core/capabilities/sync/
  test_encryption_roundtrip.py
  test_merge_lww_per_entity.py
  test_s3_backend_against_minio.py
  test_partial_failure_leaves_local_consistent.py
```

## What gets synced (the serializer is the single source of truth)

| Entity                         | Source module                            | Merge rule                                     |
|--------------------------------|------------------------------------------|------------------------------------------------|
| Memory facts (Atelier-local)   | `sqlite_memory_store.py`                 | LWW by `captured_at`, both kept in audit log   |
| Cross-vendor audit log (W0)    | `~/.atelier/memory_audit/*.jsonl`        | Append-only union by `(at, fact_id, event)`    |
| Outcome capture                | `outcome_capture.py`                     | Immutable per session — no conflicts possible  |
| Session reports                | `session_report.py`                      | Immutable once session closes                  |
| Route config (W3)              | `~/.atelier/route.yaml`                  | LWW with audit                                 |
| Pricing pin (W2)               | `~/.atelier/sync.yaml` references        | LWW with audit                                 |

**Explicitly not synced:** vendor API keys, active session events, anything
in the user's repo, anything under `~/.atelier/memory_backups/` (those are
machine-local safety copies).

## Encryption model

- User passphrase → Argon2id → 32-byte symmetric key, **never leaves the
  machine.** Key is cached in OS keyring once the user has typed it.
- Every uploaded blob is AEAD-encrypted (XChaCha20-Poly1305 default) with a
  random nonce per upload.
- Server stores ciphertext + (machine_id, entity_kind, modified_at,
  content_hash). Cleartext is never persistable on the cloud side.
- Lost passphrase = lost data. There is no recovery. **Surface this
  loudly in `sync init`.**

## Merge / conflict rules

Per-entity LWW (above). Where both sides modify the same entity, the loser
is preserved in the audit log so W1's `memory rollback` can resurrect it.

No conflict UI in v1. The diff already exists (`memory diff` from W0); the
sync log shows which side won and W1's rollback is the lever.

## Backends

- **S3-shaped (default v1):** S3, R2, Backblaze, MinIO. YAML config. No
  hosted Atelier infra required to ship.
- **Atelier Cloud:** lives in a separate repo (`atelier-cloud`). W4 ships
  the client and the contract, not the service. The service is **gated
  behind a feature flag** until at least one paying team commits.
- **SSH/SCP:** the cheap power-user backend. Same serializer, transport is
  `paramiko` + chunked upload. Ships in W4 to derisk the multi-backend
  abstraction.

## User-visible CLI

```
$ atelier sync init
Choose backend:
  1. Self-host S3 / R2 / Backblaze   (recommended for v1)
  2. SSH / SCP to your own server
  3. Atelier Cloud (currently invite-only)
> 1
S3 endpoint? https://s3.us-west-2.amazonaws.com
Bucket?      pankaj-atelier-sync
Passphrase?  (will not be echoed; cached in keyring)
✓ Machine ID: studio-mbp. Sync config saved.

$ atelier sync up
$ atelier sync down
$ atelier sync                       # bidirectional, default
$ atelier sync status
  Backend: s3 (pankaj-atelier-sync)
  Last push: 2m ago  (84 facts, 12 sessions, 3 audit events)
  Last pull: 2m ago
  Conflicts: 0
  Machines:  studio-mbp, dev-vm
```

## Validation

- `test_encryption_roundtrip` — random plaintext → encrypt → decrypt is
  identity; wrong passphrase fails with a clear error.
- `test_merge_lww_per_entity` — concurrent edits on two fake machines yield
  the documented winner and loser-in-audit-log.
- `test_s3_backend_against_minio` — full up/down round trip against a MinIO
  container in CI.
- `test_partial_failure_leaves_local_consistent` — inject a network failure
  mid-sync; local DB is untouched on the failing entity, others succeed,
  status reports which.
- `test_active_session_events_are_not_uploaded` — fixture session that is
  still running stays local even if `sync up` is called.
- `test_passphrase_cached_in_keyring_not_disk` — file probe + keyring probe.
- `test_existing_usage_sync_renamed_and_callsites_updated` — import paths
  resolve; the old name fails clearly.

## Exit criteria

- `sync init / up / down / status` work against S3 and SSH backends.
- MinIO integration test runs in CI.
- Ciphertext-only on the server side (verified by reading raw bucket
  objects in test).
- Network failure mid-sync never corrupts local state.
- `core/service/sync.py` → `usage_sync.py` rename is complete, all call
  sites updated, docs grep-clean.
- The Atelier Cloud backend exists behind a feature flag with a
  documented API contract; service implementation is out of scope.
- Trace recorded via `mcp__atelier__record`.

## Out of scope (this milestone)

- **Auto-sync timer / real-time push.** v2.
- **Visual conflict merge UI.** W6 / web dashboard.
- **Multi-user shared workspace.** W6.
- **Backup / restore tooling.** W7 owns retention; restore is `sync down`
  + W1 rollback.
- **The Atelier Cloud service itself.** Separate repo; W4 ships the
  client, contract, and feature flag only.

## Open questions

1. **Passphrase strength enforcement.** Reject weak passphrases in `sync
   init`? Default: **yes — refuse passphrases under zxcvbn score 3, force
   `--allow-weak` for testing**.
2. **Bucket layout / prefixing.** One bucket per user, or one bucket
   shared across users with prefixes? Default: **one prefix per
   `account_id`** so the same bucket can host a future team backend in W6.
3. **Migration path for users on the old `sync` name.** Nothing user-facing
   was on it; the rename is purely internal. Default: **no compat shim**
   (consistent with the "hard-remove, never deprecate" memory note).
4. **Quota.** Default: **unbounded for self-host; Atelier Cloud has 1GB per
   account, hard-limited not trial.** Spec 06's defaults stand.
