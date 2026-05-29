# Phase 20 Patterns — Runtime SCIP Indexer Provisioning

## Existing patterns

- Installer uses explicit dry-run helpers and managed runtime directories.
- SCIP binary discovery is centralized in `binaries.py`.
- `ScipIndexer.available_binaries()` is the current availability surface.

## Implementation pattern

- Extend installer package arrays/commands instead of adding Makefile wrappers.
- Keep discovery order:
  1. explicit env var
  2. Atelier-managed binary dirs
  3. system `PATH`
- Add bootstrap result dataclasses/Pydantic models with explicit statuses.
- Use subprocess/curl only through safe argv lists in future bootstrap; Phase 20 tests should verify offline fail-closed behavior without network.

## Test pattern

- Shell syntax: `bash -n scripts/install.sh`.
- Dry-run installer path verifies `scip-python` and `scip-typescript` are included.
- Unit tests create fake managed binaries and verify discovery before PATH.
- Bootstrap tests verify Tier-2 offline fail-closed and Tier-3 toolchain hint behavior.
- Availability tests verify status output per language.
