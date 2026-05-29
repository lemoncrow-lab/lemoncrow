---
phase: 22-lint-and-coverage-gates
plan: 01
subsystem: ci-quality-gates
tags: [ruff, coverage, ci, lint, prevention-gate]
requires: []
provides:
  - "Ruff BLE/T20 prevention gate (new blind-except/print debt fails make lint)"
  - "make test-full slow-inclusive coverage target with COV_FAIL_UNDER floor"
  - "nightly-coverage.yml scheduled full-suite coverage workflow"
  - "M2/M3 BLE001/T201 burn-down ledger (per-file-ignores table)"
affects:
  - pyproject.toml
  - Makefile
  - .github/workflows/nightly-coverage.yml
tech-stack:
  added: []
  patterns:
    - "Scoped rule relaxation via [tool.ruff.lint.per-file-ignores] (mirrors [[tool.mypy.overrides]] precedent)"
    - "CI delegates to Makefile targets (run: make test-full)"
    - "Schedule + workflow_dispatch only, contents: read least privilege, uv sync --frozen"
key-files:
  created:
    - .github/workflows/nightly-coverage.yml
  modified:
    - pyproject.toml
    - Makefile
decisions:
  - "COV_FAIL_UNDER set to 66 (conservative provisional floor) — full local measurement could not complete; calibrate against first CI nightly run"
  - "BLE/T20 added to select; existing 96 BLE001 + 19 T201 files parked in per-file-ignores, NOT in top-level ignore (stays ['E501'])"
metrics:
  duration: ~75m (dominated by coverage-measurement attempts)
  completed: 2026-05-29
---

# Phase 22 Plan 01: Lint and Coverage Gates Summary

Installed Phase 22 prevention gates — Ruff `BLE001`/`T20` now fail `make lint` for **new**
blind-except/print debt while 96 existing BLE001 files + 19 T201 files are parked in a
scoped `[tool.ruff.lint.per-file-ignores]` burn-down ledger; added a slow-inclusive
`make test-full` coverage target and a scheduled `nightly-coverage.yml` workflow that
enforces the floor.

## What Was Built

| Surface | Change |
|---------|--------|
| `pyproject.toml` | `select` extended to include `"BLE"`, `"T20"`; new `[tool.ruff.lint.per-file-ignores]` table with 96 BLE001 + 19 T201 entries (12 combined). `ignore` unchanged at `["E501"]`. |
| `Makefile` | `COV_FAIL_UNDER ?= 66` variable; `test-full` added to `.PHONY`; new `test-full` target running `uv run pytest -m "" --cov=atelier --cov-report=term-missing --cov-fail-under=$(COV_FAIL_UNDER)`. Fast path (`test`, `test-cov`, `addopts = -m 'not slow'`) untouched. |
| `.github/workflows/nightly-coverage.yml` | New workflow: `schedule` (cron `"0 7 * * *"`) + `workflow_dispatch` only, `permissions: contents: read`, `uv sync --frozen --group dev`, `timeout-minutes: 40`, final step `run: make test-full`. |

## Coverage Measurement (QBL-GATE-04 — measured, with documented limitations)

**Measurement environment:** local **dirty worktree** (151 modified/deleted files per
`git status`), branch `cc`, **no Postgres / worker services available**.

**Measured value:** A partial parallel run (`pytest -m "not slow" -n auto --dist=loadfile`,
service-gated `test_postgres_store.py` + `test_worker_jobs.py` excluded) reached ~93% of the
non-slow subset and combined to **68% TOTAL** before xdist workers crashed. Because adding
tests can only raise the coverage ratio (numerator grows, denominator fixed), **68% is a
strict lower bound**; the true full slow-inclusive figure the nightly job will report is
≥ 68%.

**Chosen `COV_FAIL_UNDER`: 66** — ~2 points below the measured lower bound, deliberately
conservative so dirty-worktree / environment drift cannot cause false nightly failures.

**Why the full slow-inclusive run could not complete locally (Pitfall 1 + Pitfall 6):**
- **Serial** `pytest -m ""` (and `-m "not slow"`) **hung at ~14%** — a test blocks on an
  unavailable service. Three independent serial runs reproduced the hang.
- **Parallel** (`-n auto`) progressed quickly but hit repeated
  `pyo3_runtime.PanicException: _native::Parser is unsendable, but sent to another thread`
  from `src/atelier/infra/tree_sitter/tags.py` (tree-sitter Rust parser not thread-safe under
  xdist), crashing workers and stalling at ~93%. This is a **pre-existing environment /
  parallelism issue unrelated to this config-only phase** — no runtime source was changed.

**Action for M-after / CI:** The first `nightly-coverage.yml` run executes on clean `main`
with CI services; read its reported TOTAL and raise `COV_FAIL_UNDER` to ~2 points below it.
The `COV_FAIL_UNDER ?=` form allows override without editing the file
(`make test-full COV_FAIL_UNDER=NN`).

## Slow-Inclusive Collection (QBL-GATE-03)

`uv run pytest --collect-only -m ""` collects **2092** tests vs **2005** for the default
filter (87 slow tests deselected by `addopts = -m 'not slow'`). `-m ""` correctly overrides
the marker filter — the override-ini fallback (Assumption A1) was **not** needed. Note: the
plan's stale estimate was 2088 (2001+87); the live tree is 2092 (2005+87) — the suite grew
slightly. The 87 slow-test delta is unchanged.

## BLE001 / T201 Final Counts (QBL-GATE-02 + QBL-GATE-05)

Re-derived live from source (worktree dirty, so regenerated rather than trusting RESEARCH.md):

| Bucket | Count |
|--------|-------|
| Files with both codes (`["BLE001", "T201"]`) | 12 |
| Files with `["BLE001"]` only | 84 |
| Files with `["T201"]` only | 7 |
| **Total BLE001 files** | **96** |
| **Total T201 files** | **19** |

These match the RESEARCH.md worklists exactly. The table **is** the M2/M3 burn-down ledger.
Because `ruff check --select BLE001` now honors per-file-ignores, the raw worklist re-derives
via `--config 'lint.per-file-ignores={}'` (confirmed: 96 BLE001 + 19 T20 files), or simply by
reading the table.

## Verification Results

- ✅ `uv run ruff check src` → exit 0 (clean tree; existing debt parked, QBL-GATE-01/02).
- ✅ Gate probe: a throwaway `src/atelier/_gate_probe.py` with `print()` + bare
  `except Exception` reported **both** BLE001 and T201, then deleted (not committed) —
  proves new debt in a clean file fails lint (QBL-GATE-01).
- ✅ Top-level `ignore = ["E501"]` unchanged (no BLE001/T201 blanket disable, QBL-GATE-02).
- ✅ `select` contains `"BLE"` and `"T20"`; `[tool.ruff.lint.per-file-ignores]` present with
  the combined `["BLE001", "T201"]` entry for `mcp_server.py`.
- ✅ `make test-full` exists, in `.PHONY`, listed in `make help`; collects 2092 slow-inclusive
  tests; enforces `--cov-fail-under=$(COV_FAIL_UNDER)` (QBL-GATE-03).
- ✅ Fast PR path untouched: `addopts = "-ra --strict-markers -m 'not slow'"` intact.
- ✅ `nightly-coverage.yml` parses; `schedule` present; `permissions.contents == read`;
  `workflow_dispatch` present; `make test-full` + `uv sync --frozen --group dev` present;
  no `pull_request`/`push` triggers; cron `0 7 * * *` distinct from docs-governance's
  `25 3 * * *` (QBL-GATE-04). `actionlint` not installed locally — YAML validated via PyYAML.
- ⚠️ `make test-full` was **not** executed to green locally (full suite cannot complete in
  this environment — see Coverage Measurement). It will run for real on the first nightly CI.

## Deviations from Plan

### Auto-fixed / adjustments
- **[Rule 3 - blocking, env] Coverage floor measured on a partial subset, not the full suite.**
  - Found during: Task 2. The full slow-inclusive run hangs (serial) / panics (parallel)
    locally due to missing services and a tree-sitter xdist thread-safety panic.
  - Resolution: used the plan's explicit fallback — measured the largest completable subset
    (68% lower bound) and set a conservative floor (66) flagged for CI calibration. No runtime
    source modified.
- **Collection count is 2092, not the plan's stated 2088.** The live test tree grew; the
  slow delta (87) is unchanged. `-m ""` override works as designed; no fallback needed.

## Threat Surface

No new network endpoints, auth paths, or schema changes — config/CI-only phase. Threat
register mitigations applied: scoped per-file-ignores (T-22-01), `uv sync --frozen` (T-22-02),
`contents: read` least privilege (T-22-03). No packages installed (T-22-SC N/A).

## Known Stubs

None — this is a config/CI phase with no runtime stubs.

## Self-Check: PASSED

- FOUND: pyproject.toml (modified — BLE/T20 select + per-file-ignores table)
- FOUND: Makefile (modified — COV_FAIL_UNDER + test-full target)
- FOUND: .github/workflows/nightly-coverage.yml (created)
- FOUND: .planning/phases/22-lint-and-coverage-gates/22-01-SUMMARY.md (this file)
- Commit hash recorded in the orchestrator completion message below.
