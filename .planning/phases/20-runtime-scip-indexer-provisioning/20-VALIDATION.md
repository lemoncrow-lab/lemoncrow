# Phase 20 Validation — Runtime SCIP Indexer Provisioning

## Focused checks

- `bash -n scripts/install.sh`
- `ATELIER_DRY_RUN=1 bash scripts/install.sh --help` or equivalent non-mutating dry-run path
- `uv run pytest tests/infra/code_intel/scip -q`
- `uv run ruff check src/atelier/infra/code_intel/scip scripts/install.sh tests/infra/code_intel/scip`
- `uv run mypy --strict src/atelier/infra/code_intel/scip`

## Acceptance criteria

- Install-time managed Node packages include `scip-python` and `scip-typescript`.
- SCIP discovery searches Atelier-managed binary directories before system `PATH`.
- Tier-2 bootstrap fails closed offline when no checksum-allowlisted source is available.
- Tier-3 Rust/Java entries return user-toolchain-required status rather than auto-installing.
- Availability output reports each SCIP language’s readiness or missing/provisioning state.
