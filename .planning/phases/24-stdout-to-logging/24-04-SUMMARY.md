---
phase: 24-stdout-to-logging
plan: 04
subsystem: lint-config
tags: [ruff, t201, stdout, logging, config]
requires: ["24-02", "24-03"]
provides: ["T201 boundary enforced at src/benchmarks/** only", "final print() inventory recorded"]
affects: ["pyproject.toml [tool.ruff.lint.per-file-ignores]"]
tech-stack:
  added: []
  patterns: ["per-file-ignores encode the CLI/non-CLI stdout boundary"]
key-files:
  created:
    - .planning/phases/24-stdout-to-logging/24-04-SUMMARY.md
  modified:
    - pyproject.toml
decisions:
  - "Retained explicit per-file T201 entries for the 7 benchmark dev-CLIs (did not collapse to a glob) to keep the boundary auditable and preserve per-file BLE001 entries cleanly"
requirements: [QBL-LOG-01, QBL-LOG-03]
metrics:
  duration: ~15m
  completed: 2026-05-29
---

# Phase 24 Plan 04: Stdout Lint Boundary Summary

Shrank Ruff T201 `per-file-ignores` from 19 entries to the 7 benchmark dev-CLI files under `src/benchmarks/**`, locking the CLI/non-CLI stdout boundary in config (QBL-LOG-03) and recording the final `print()` inventory (QBL-LOG-01).

## What Was Done

### Task 1 — Shrink T201 per-file-ignores to the benchmark boundary
- **Demoted** 9 now-converted files from `["BLE001", "T201"]` → `["BLE001"]` (kept BLE001 burn-down debt, dropped T201): `mcp_server.py`, `registry.py`, and session parsers `_common.py`, `claude.py`, `cline.py`, `codex.py`, `copilot.py`, `gemini.py`, `opencode.py`.
- **Removed** the entry entirely for 3 T201-only converted files: `goose.py`, `kiro.py`, `src/atelier/infra/benchmarks/publisher.py`.
- **Retained** the 7 benchmark dev-CLI entries under `src/benchmarks/**`:
  - `swe/routing_replay_bench.py`, `swe/savings_replay.py`, `tool_bench/report.py` (kept both `BLE001` + `T201`).
  - `code_intel/scale_decision_eval.py`, `swe/savings_bench.py`, `swe/swebench_eval.py`, `tool_bench/__main__.py` (`T201` only).
- Chose to keep **explicit per-file entries** rather than the optional `"src/benchmarks/**/*.py" = ["T201"]` glob — the boundary stays fully auditable and the three benchmark `BLE001` entries remain cleanly separated.
- Updated the surrounding comments to document that `src/benchmarks/**` stdout is the intentional report channel (Phase 24, QBL-LOG-03).
- **Commit:** `4d7b5df`

### Task 2 — Final boundary verification + inventory record
Ran the full Phase 24 validation set and recorded the final inventory below.

## Final print() Inventory (QBL-LOG-01)

With per-file-ignores disabled, all remaining T201 (`print`) findings are confined to `src/benchmarks/**` (CLI-allowed report channel). No converted non-CLI file carries any T201 finding.

| Bucket | File | print() count |
|--------|------|---------------|
| CLI-allowed (`src/benchmarks/**`) | `tool_bench/report.py` | 44 |
| CLI-allowed (`src/benchmarks/**`) | `tool_bench/__main__.py` | 4 |
| CLI-allowed (`src/benchmarks/**`) | `swe/routing_replay_bench.py` | 2 |
| CLI-allowed (`src/benchmarks/**`) | `swe/savings_bench.py` | 2 |
| CLI-allowed (`src/benchmarks/**`) | `swe/swebench_eval.py` | 2 |
| CLI-allowed (`src/benchmarks/**`) | `code_intel/scale_decision_eval.py` | 1 |
| CLI-allowed (`src/benchmarks/**`) | `swe/savings_replay.py` | 1 |
| **Total CLI-allowed** | | **56** |
| Converted non-CLI (logging/sys.stdout.write) | (none — 0 T201 findings) | 0 |

## Validation Results

| # | Command | Result |
|---|---------|--------|
| 1 | `uv run ruff check src --select T20 --config 'lint.per-file-ignores={}'` | ✅ Only `src/benchmarks/**` files flagged (7 files, 56 finds). Note: default output format prints rule message and file path on separate lines; verified with `--output-format=concise` for accurate per-file attribution. |
| 2 | `uv run ruff check src --select T20` | ✅ All checks passed |
| 3 | `uv run ruff check src` | ✅ All checks passed |
| 4 | `uv run pytest tests/gateway/test_mcp_stdio_smoke.py -m "" -q` | ✅ 1 passed |
| 5 | `uv run pytest tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_jsonrpc_e2e.py -q` | ⚠️ 43 passed, 1 failed (unrelated baseline — see below) |
| 6 | `uv run pytest tests/gateway/test_cli*.py -k import -q` | ✅ 6 passed, 92 deselected |

### Verification command note
The plan's Task 1 verify snippet greps `T201` against Ruff's **default** output format, which emits the rule message (`T201 \`print\` found`) and the file path on separate lines — so `grep -vE 'src/benchmarks/'` does not correctly filter by file. Verification was performed with `--output-format=concise` (`file:line:col: T201 ...`), which confirms all 7 flagged files are under `src/benchmarks/**` and zero converted non-CLI files remain.

## Deviations from Plan

None for implementation — `pyproject.toml` was the only file modified, exactly as scoped.

Verification method adjusted to use `--output-format=concise` (see note above) because the plan's default-format grep cannot attribute rule codes to files. This is a verification-accuracy fix, not a code change.

## Unrelated Baseline Failures (NOT fixed — out of Phase 24 scope)

- **`tests/gateway/test_mcp_jsonrpc_e2e.py::test_tools_list_matches_registered_surface`** — fails because the dirty working tree registers extra MCP tools (`usages`, `pattern`) that are not in the test's `EXPECTED_TOOLS` set. This is a pre-existing tool-surface mismatch in the working tree, unrelated to the lint-config change (a Ruff config edit cannot affect runtime tool registration). Per 24-VALIDATION "Known Baseline", recorded but not fixed.

## Threat Mitigations Applied

- **T-24-09 (Tampering — ignore scope):** T201 ignores shrunk to `src/benchmarks/**` only; ignores-disabled grep confirms no non-CLI file can mask a new stdout leak.
- **T-24-10 (Repudiation — boundary intent):** `src/benchmarks/**` stdout rationale documented in `pyproject.toml` comments; final buckets recorded above.

## Self-Check: PASSED

- `pyproject.toml` modified and committed: FOUND (`4d7b5df`)
- `.planning/phases/24-stdout-to-logging/24-04-SUMMARY.md`: FOUND
- Commit `4d7b5df`: present in git log
