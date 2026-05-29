# Phase 20 Research — Runtime SCIP Indexer Provisioning

## Source

`docs/plans/dedicated-language-support/M5-scip-runtime-provisioning.md`

## Findings

- `scripts/install.sh` already provisions a Node runtime under Atelier-managed directories and installs npm globals with `--prefix`.
- Phase 19 added explicit SCIP binary specs and an opt-in `ScipIndexer.index_language(...)` runner.
- `discover_scip_binary()` currently checks env override and then `PATH`; it does not search Atelier-managed binary directories.
- Requirements split provisioning into:
  - Tier 1: install-time `scip-python` and `scip-typescript` via managed Node/npm.
  - Tier 2: lazy, checksum-verified fetch for cheaper standalone indexers.
  - Tier 3: detect/document heavy toolchain-backed indexers without auto-install.

## Decisions

- Add Tier-1 npm indexers to the installer’s existing managed Node global install path.
- Add managed binary directory discovery before system `PATH` while preserving env override precedence.
- Add a `scip/bootstrap.py` surface with explicit tier metadata and fail-closed bootstrap results.
- Do not download arbitrary binaries without checksum metadata; Tier-2 bootstrap will only install when a checksum-allowlisted source exists, otherwise it returns an offline/unavailable result without side effects.
- Add availability/status output through SCIP APIs so callers can distinguish ready, bootstrap-available, missing, and user-toolchain-required states.

## Risks

- Installer shell changes must be syntax-checked and dry-run safe.
- Lazy bootstrap must not execute shell strings or leave partial binaries.
- Tier-3 guidance must not imply Atelier auto-installs Rust/JDK/coursier.
