# Phase 21 Validation Plan

## Goal

Prove DLS-VAL-01 through DLS-VAL-04 with focused tests, committed benchmark artifacts, docs checks, and a final verification report.

## Checks

1. Language matrix:
   - `uv run pytest tests/core/test_language_matrix.py -q`
2. SCIP availability:
   - `uv run pytest tests/infra/code_intel/scip/test_scip_availability_report.py -q`
3. Outline savings benchmark:
   - `LOCAL=1 uv run pytest tests/benchmarks/test_dls_outline_savings.py -m ab -q`
4. Static validation:
   - `uv run ruff check tests/core/test_language_matrix.py tests/infra/code_intel/scip/test_scip_availability_report.py tests/benchmarks/test_dls_outline_savings.py`
   - `uv run mypy --strict tests/core/test_language_matrix.py tests/infra/code_intel/scip/test_scip_availability_report.py tests/benchmarks/test_dls_outline_savings.py`
5. Docs:
   - `make docs-check && make check-agent-context`

## Completion Criteria

- Fixture matrix covers all canonical languages.
- Benchmark artifact records full, generic, dedicated, and guard behavior for `bash`, `yaml`, `toml`, `json`, and `sql`.
- SCIP availability report matches bootstrap metadata.
- Public docs reflect shipped language support and provisioning tiers.

