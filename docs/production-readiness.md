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
  - `/config` (authenticated)
- Run ledger persistence verified in `.atelier/runs/`.
- Trace ingestion verified via `atelier trace record` and `/v1/traces`.
- Analytics summary checked via `/analytics/summary` or the dashboard summary endpoint.
- Background controller status reviewed with `atelier servicectl status`.

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
- Dev-mode-only retrieval and rubric tools are gated behind `ATELIER_DEV_MODE=1` where expected.
- Remote MCP mode tested with explicit API key boundary.

## Scaling Guidance

- SQLite is single-node/local development only.
- Use Postgres backend for multi-agent or service deployments.
- Enable worker process for queued jobs in production.
- Periodically archive old traces/runs to control storage growth.

## Knowledge Bundle Governance

- Built-in seed blocks under `src/atelier/infra/seed_blocks/` and built-in rubrics under `src/atelier/core/rubrics/` remain source-controlled artifacts.
- Domain bundle metadata exposed through `atelier domain list` and `atelier domain info` should match the shipped content.
- New or updated knowledge artifacts require a clean `atelier init` against a fresh store plus targeted benchmark or eval evidence when they affect routing, retrieval, or savings claims.
- `atelier benchmark packs` remains the benchmark-only coverage surface; there is no public `atelier pack install` workflow on the current CLI.
- Runtime-learned ReasonBlocks are review/promote candidates, not auto-published governance records.

## Release Sign-Off

- [ ] T1 full system validation completed
- [ ] T2 golden dogfood scenarios completed
- [ ] T3 benchmark suite completed
- [ ] T4 install/deploy verification completed
- [ ] T5 documentation audit completed
- [ ] T6 checklist fully reviewed and signed
