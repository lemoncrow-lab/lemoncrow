# Phase 20 Verification — Runtime SCIP Indexer Provisioning

## Verdict

PASS — Phase 20 satisfies DLS-PROV-01 through DLS-PROV-05.

## Evidence

- `scripts/install.sh` installs `scip-python` and `scip-typescript` alongside existing managed npm tooling under `$ATELIER_NODE_DIR`.
- `discover_scip_binary()` preserves env override precedence, then searches Atelier-managed dirs before system `PATH`.
- `ensure_scip_binary()` supports checksum-gated Tier-2 lazy fetches into `ATELIER_ROOT/bin`, fails closed without checksum metadata, and removes partial downloads on failure.
- Rust and Java remain user-toolchain-required with explicit install hints instead of heavy auto-install.
- `ScipIndexer.availability_statuses()` exposes ready, missing install-time, bootstrap-unavailable, and user-toolchain-required states; bootstrap context renders those statuses.

## Validation

- `bash -n scripts/install.sh`
- `uv run pytest tests/infra/code_intel/scip -q` — 35 passed, 5 skipped
- `uv run ruff check src/atelier/infra/code_intel/scip tests/infra/code_intel/scip`
- `uv run mypy --strict src/atelier/infra/code_intel/scip`

## Commits

- `31c9f43 feat(20): provision scip runtime indexers`
- `7736d1e feat(20): add checksum-gated scip bootstrap`

