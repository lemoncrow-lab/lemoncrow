---
phase: 24-stdout-to-logging
verified: 2026-05-29T00:00:00Z
status: passed
score: 4/4 must-haves verified
overrides_applied: 0
re_verification: false
---

# Phase 24: Stdout to Logging Verification Report

**Phase Goal:** Replace stray `print()` in non-CLI runtime/server/background/core/infra
modules with logging (or stderr diagnostics) so MCP stdio JSON-RPC framing cannot be
corrupted; preserve legitimate CLI/benchmark stdout behind an explicit T20 lint boundary.
**Verified:** 2026-05-29
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (Requirement Gates)

| # | Truth (Requirement) | Status | Evidence |
| --- | --- | --- | --- |
| 1 | QBL-LOG-01 — Final `print()` inventory recorded; with ignores disabled all T201 findings confined to `src/benchmarks/**` | ✓ VERIFIED | `ruff check src --select T20 --config 'lint.per-file-ignores={}'` → exit 1, **exactly 56 findings**, all under `src/benchmarks/**` (0 outside). Per-file counts match 24-04-SUMMARY inventory (report.py 44, __main__.py 4, routing_replay 2, savings_bench 2, swebench_eval 2, scale_decision_eval 1, savings_replay 1 = 56). |
| 2 | QBL-LOG-02 — Non-CLI runtime prints (gateway/session-parsers/publisher/registry/mcp_server) replaced by module logging or explicit stderr | ✓ VERIFIED | `grep -c '\bprint('` = **0** across `registry.py`, `infra/benchmarks/publisher.py`, and all `session_parsers/*.py`. Module loggers present (`getLogger(__name__)`) in registry.py:14, publisher.py:16. `mcp_server.py` `--version` uses `sys.stdout.write` (L5456) with early `return` preserved before the JSON-RPC loop. CLI import progress routed to a single idempotent INFO stderr StreamHandler in `cli/app.py` (`_ensure_import_progress_logging`, L74; `setStream` refresh L91). |
| 3 | QBL-LOG-03 — CLI/benchmark stdout boundary explicit; `ruff check src` & `--select T20` clean; benchmarks the only T201 findings | ✓ VERIFIED | `ruff check src --select T20` → **All checks passed!** (exit 0). `ruff check src` → **All checks passed!** (exit 0). `pyproject.toml` per-file-ignores shrunk to 7 `src/benchmarks/**` entries (L121–216); 9 converted files demoted to BLE001-only, 3 (goose, kiro, publisher) removed entirely. |
| 4 | QBL-LOG-04 — MCP stdio smoke strictly rejects every non-empty stdout line that is not JSON-object framing | ✓ VERIFIED | `tests/gateway/test_mcp_stdio_smoke.py` L80–84: each non-empty line parsed with `json.loads` and `isinstance(msg, dict)` asserted; non-JSON raises `AssertionError("non-protocol stdout line: …")`. Test runs: `pytest test_mcp_stdio_smoke.py -m "" -q` → **1 passed**. |

**Score:** 4/4 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
| --- | --- | --- | --- |
| `tests/gateway/test_mcp_stdio_smoke.py` | Strict per-line JSON-object framing gate | ✓ VERIFIED | Strict parse L80–84; test passes |
| `src/atelier/gateway/hosts/session_parsers/*.py` | Module loggers, zero prints | ✓ VERIFIED | 0 prints across all parser files |
| `src/atelier/gateway/cli/app.py` | Idempotent stderr progress handler | ✓ VERIFIED | `_ensure_import_progress_logging` wired into 5 import command paths |
| `src/atelier/gateway/hosts/registry.py` | Logger for host-load failures | ✓ VERIFIED | `logger.warning` replaces print; 0 prints |
| `src/atelier/infra/benchmarks/publisher.py` | Logger for dry-run lines | ✓ VERIFIED | `logger.info` for dry-run; 0 prints |
| `src/atelier/gateway/adapters/mcp_server.py` | `--version` via stdout.write, pre-loop return | ✓ VERIFIED | L5456 `sys.stdout.write`; `return` preserved |
| `pyproject.toml` | T201 ignores limited to `src/benchmarks/**` | ✓ VERIFIED | 7 benchmark entries only |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| --- | --- | --- | --- |
| T201 boundary (ignores disabled) | `ruff check src --select T20 --config 'lint.per-file-ignores={}' --output-format=concise` | exit 1, 56 findings, all `src/benchmarks/**` | ✓ PASS |
| T201 normal config | `ruff check src --select T20` | All checks passed (exit 0) | ✓ PASS |
| Full lint | `ruff check src` | All checks passed (exit 0) | ✓ PASS |
| MCP stdio framing | `pytest tests/gateway/test_mcp_stdio_smoke.py -m "" -q` | 1 passed | ✓ PASS |
| CLI import suite | `pytest tests/gateway/test_cli*.py -k import -q` | 6 passed, 92 deselected | ✓ PASS |
| MCP surfaces + e2e | `pytest tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_jsonrpc_e2e.py -q` | 43 passed, 1 failed (unrelated baseline) | ✓ PASS (in scope) |

### Requirements Coverage

| Requirement | Description | Status | Evidence |
| --- | --- | --- | --- |
| QBL-LOG-01 | Record final print inventory; T201 confined to benchmarks | ✓ SATISFIED | 56 findings, 100% under `src/benchmarks/**` |
| QBL-LOG-02 | Convert non-CLI runtime prints to logging/stderr | ✓ SATISFIED | 0 prints in all targeted runtime/infra files; loggers + stderr handler wired |
| QBL-LOG-03 | Explicit CLI/benchmark stdout boundary; lint clean | ✓ SATISFIED | `ruff check src` + `--select T20` pass; pyproject boundary shrunk to 7 benchmark entries |
| QBL-LOG-04 | Strict MCP stdio framing gate | ✓ SATISFIED | Strict json.loads + isinstance(dict) gate; smoke test passes |

### Anti-Patterns Found

None blocking. The strict framing gate in the smoke test was hardened from a lenient
`try/except: pass` to a re-raising `AssertionError` (commit `5f5c607` review fix) — this is
stronger than, not weaker than, the 24-01-SUMMARY description ("bare json.loads"); the gate
strictly fails on any non-protocol stdout line. No debt markers (TBD/FIXME/XXX) introduced in
Phase 24 files.

### Unrelated Baseline (NOT a Phase 24 gap)

`tests/gateway/test_mcp_jsonrpc_e2e.py::test_tools_list_matches_registered_surface` fails with
`pyo3_runtime.PanicException: _native::Parser is unsendable, but sent to another thread`
originating in `src/atelier/infra/tree_sitter/tags.py:160` (`parser.parse`). This is a
pre-existing tree-sitter/pyo3 threading defect. `tree_sitter/tags.py` is not in Phase 24's
file scope and does not appear in the print→logging change set; the failure path is
unrelated to stdout framing. Consistent with 24-01/24-03/24-04 SUMMARY "Known Baseline"
documentation. Out of scope for this phase.

### Human Verification Required

None. The plan's "manually smoke the MCP server and confirm no banner/log leaks to stdout"
is fully covered by the automated strict framing gate (`test_mcp_stdio_smoke.py`), which
launches a real `atelier-mcp` subprocess and asserts every stdout line is JSON-object framing.

### Gaps Summary

No gaps. All four requirement gates (QBL-LOG-01 through QBL-LOG-04) are independently
verified against the codebase, not merely SUMMARY claims:
- 56 T201 findings, all confined to `src/benchmarks/**` (the sanctioned dev-CLI report channel).
- Zero `print()` remain in any targeted non-CLI runtime/server/infra module.
- `ruff check src` and `ruff check src --select T20` both pass.
- The MCP stdio framing gate strictly rejects non-protocol stdout and passes.
- All six Phase 24 implementation commits present in git history.

The single failing e2e test is an unrelated pre-existing tree-sitter threading panic, not a
Phase 24 regression.

---

_Verified: 2026-05-29_
_Verifier: the agent (gsd-verifier)_
