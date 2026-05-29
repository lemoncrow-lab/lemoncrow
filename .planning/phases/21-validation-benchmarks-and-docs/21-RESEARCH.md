# Phase 21 Research — Validation, Benchmarks, and Docs

## Summary

Phase 21 closes the v0.4 Dedicated Language Support milestone by proving the shipped language matrix, outline savings behavior, SCIP provisioning status, and user-facing documentation match reality.

## Key Findings

- The canonical language registry is `src/atelier/infra/code_intel/languages.py`; validation should derive languages from `LANGUAGES` instead of duplicating extension maps.
- Dedicated outline support is configured by `SUPPORTED_LANGUAGES` in `treesitter_ast.py`. `smart_read()` applies a 25% savings guard and can intentionally degrade to generic/full output.
- Tag extraction should use `extract_tags_from_text()`; data languages are definition-only and JSON tags are bounded to top-level keys.
- Existing one-off outline/tag tests prove individual languages; Phase 21 should consolidate representative fixtures into a matrix.
- SCIP availability is centralized in `scip_availability_statuses()` / `ScipIndexer.availability_statuses()`, including Tier-1 install-time, Tier-2 checksum-gated bootstrap, and Tier-3 user-toolchain statuses.
- Existing reports live under `reports/<week>/` with `reports/index.json`; DLS benchmark artifacts should follow that committed-report convention.

## Implementation Guidance

1. Add shared language fixtures under `tests/fixtures/languages/`.
2. Add a parametrized matrix test for detection, expected outline kind, and non-empty definition tags where tree-sitter definitions apply.
3. Add an `ab` benchmark test for `bash`, `yaml`, `toml`, `json`, and `sql`, measuring full vs generic vs dedicated outputs and the 25% guard.
4. Commit a benchmark artifact under `reports/2026-W22/`.
5. Add SCIP availability report tests using bootstrap metadata, not shell probing.
6. Update README, QUICK_REFERENCE, architecture, and installation docs to distinguish shipped support tiers from aspirational support.

## Validation Commands

- `uv run pytest tests/core/test_language_matrix.py tests/infra/code_intel/scip/test_scip_availability_report.py -q`
- `LOCAL=1 uv run pytest tests/benchmarks/test_dls_outline_savings.py -m ab -q`
- `uv run ruff check tests/core/test_language_matrix.py tests/infra/code_intel/scip/test_scip_availability_report.py tests/benchmarks/test_dls_outline_savings.py`
- `uv run mypy --strict tests/core/test_language_matrix.py tests/infra/code_intel/scip/test_scip_availability_report.py tests/benchmarks/test_dls_outline_savings.py`
- `make docs-check && make check-agent-context`

