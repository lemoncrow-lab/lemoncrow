# Phase T Hardening Execution

This document maps the Phase T1-T6 objective to the current small Makefile surface.

## Run All Phases

```bash
make verify
make benchmark
```

`make verify` runs code checks, runtime smoke tests, and host integration checks.
`make benchmark` runs the full benchmark suite.

## T1 — Full System Validation

```bash
make verify
```

Covers:

- core checks (plan checker, retriever, rubric, rescue/failure analyzer, context compressor, monitors, run ledger, savings)
- storage checks (SQLite + Postgres-gated tests)
- host adapter checks
- service + worker checks
- pack lifecycle checks
- security checks (redaction + abuse defenses)

## T2 — Golden Dogfood Scenarios

```bash
uv run pytest tests/test_golden_fixtures.py -v
```

Runs `tests/test_golden_fixtures.py` for real scenario validation.

## T3 — Benchmark Suite

```bash
make benchmark
```

These commands correspond to:

- `atelier benchmark core`
- `atelier benchmark hosts`
- `atelier benchmark packs`
- `atelier benchmark full`

## T4 — Install + Deploy Verification

```bash
bash scripts/verify_atelier_service.sh
bash scripts/verify_atelier_mcp_stdio.sh
bash scripts/verify_atelier_postgres.sh
bash scripts/verify_agent_clis.sh
```

Runs:

- `scripts/verify_atelier_service.sh`
- `scripts/verify_atelier_mcp_stdio.sh`
- `scripts/verify_atelier_postgres.sh`
- `scripts/verify_agent_clis.sh`

## T5 — Documentation Audit

```bash
uv run pytest tests/test_docs.py -v
```

Runs docs integrity tests from `tests/test_docs.py`.

## T6 — Production Readiness Checklist

```bash
test -s docs/production-readiness.md
```

Validates presence and structure of `docs/production-readiness.md`.
