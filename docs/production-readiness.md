# Production Readiness Checklist

This checklist is the release gate for Atelier Phase D hardening.

## Deployment Checklist

- `uv sync --all-extras` completed in a clean environment.
- `make verify` passes (ruff, black --check, mypy --strict, pytest, runtime smoke tests, host checks).
- `make benchmark` passes when benchmark evidence is required for the release.
- Service config reviewed with `atelier service config`.
- `ATELIER_REQUIRE_AUTH=true` for non-local environments.
- `ATELIER_API_KEY` set for service environments.

## Backups

- SQLite deployments:
  - Backup `.atelier/atelier.db` and `.atelier/runs/` before upgrade.
- Postgres deployments:
  - Run database backup before migrations.
  - Verify restore in a staging database before production rollout.
- Keep at least one pre-upgrade snapshot and one post-upgrade snapshot.

## Migrations

- Run migrations in staging first.
- Validate backwards-compatible reads before traffic cutover.
- Verify rollback path is tested for the target release.
- For Postgres, verify schema with `bash scripts/verify_atelier_postgres.sh`.

## Observability

- Service health endpoints verified:
  - `/health`
  - `/ready`
- Run ledger persistence verified in `.atelier/runs/`.
- Trace ingestion verified via `record_trace` and list/get trace endpoints.
- Savings metrics endpoint checked (`/v1/metrics/savings`).

## Logging

- Structured logs are enabled for service and worker processes.
- Error logs include enough context for:
  - trace ID
  - domain
  - action
- Sensitive values are redacted before persistence.

## Incident Recovery

- Documented rollback command path for current release.
- Service restart procedure verified.
- Worker restart procedure verified.
- Recovery drill includes:
  - failed plan/rubric path
  - trace quarantine path
  - store recovery path

## Security Hardening

- Secret redaction tests pass (`tests/test_redaction.py`, `tests/test_security.py`).
- Shell injection checks pass in MCP tool paths.
- API auth enforced in non-local mode.
- Malformed pack rejection validated in pack validation/install tests.
- Remote MCP mode tested with explicit API key boundary.

## Scaling Guidance

- SQLite is single-node/local development only.
- Use Postgres backend for multi-agent or service deployments.
- Enable worker process for queued jobs in production.
- Periodically archive old traces/runs to control storage growth.

## Pack Governance

- Official packs must be versioned and schema-valid.
- Pack dependency constraints must resolve internally.
- New/updated pack artifacts require:
  - validation (`pack validate`)
  - benchmark (`atelier benchmark packs`)
  - host bootstrap compatibility check
- Runtime-learned ReasonBlocks are review/promote candidates, not auto-published governance records.

## Release Sign-Off

- [ ] T1 full system validation completed
- [ ] T2 golden dogfood scenarios completed
- [ ] T3 benchmark suite completed
- [ ] T4 install/deploy verification completed
- [ ] T5 documentation audit completed
- [ ] T6 checklist fully reviewed and signed
