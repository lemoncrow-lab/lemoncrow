---
phase: 24-stdout-to-logging
plan: 01
subsystem: gateway/mcp-stdio-test
tags: [testing, mcp, jsonrpc, framing, QBL-LOG-04]
requires: []
provides:
  - "Strict per-line JSON-object framing gate over atelier-mcp stdout (QBL-LOG-04)"
affects:
  - "24-02 / 24-03 print→logger conversions (now verifiably safe behind this gate)"
tech-stack:
  added: []
  patterns:
    - "Strict per-line json.loads + isinstance(dict) assertion (modeled on test_mcp_jsonrpc_e2e.py L306)"
key-files:
  created: []
  modified:
    - tests/gateway/test_mcp_stdio_smoke.py
decisions:
  - "Replaced lenient try/except:pass stdout parse with strict json.loads + isinstance(dict) assertion"
metrics:
  duration: "~6 min"
  completed: "2026-05-29"
requirements: [QBL-LOG-04]
---

# Phase 24 Plan 01: Harden MCP Stdio Smoke Framing Summary

Strict per-line `json.loads` + `isinstance(msg, dict)` assertion over `atelier-mcp`
stdout, replacing the lenient `try/except: pass` swallow so any stray non-protocol
byte now fails the smoke test — establishing the QBL-LOG-04 framing gate before the
24-02/24-03 print→logger conversions rely on it.

## What Was Built

- Hardened the stdout parse loop in `tests/gateway/test_mcp_stdio_smoke.py` (formerly
  L78-83). Every non-empty stdout line is now parsed with a bare `json.loads(line)`
  (no surrounding try/except) and asserted to be a JSON object via
  `assert isinstance(msg, dict), f"non-protocol stdout line: {line!r}"`. Only then is
  `msg["id"]` indexed into the responses map.
- Subprocess launch, request batch, and existing response assertions are unchanged —
  only stdout-parsing strictness changed.
- `@pytest.mark.slow` marker preserved.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Harden stdio smoke parse to strict JSON-object assertion | (see final commit) | tests/gateway/test_mcp_stdio_smoke.py |

## Validations Run

| Command | Result |
|---------|--------|
| `uv run pytest tests/gateway/test_mcp_stdio_smoke.py -m "" -q` | ✅ 1 passed in 3.02s |
| `uv run pytest tests/gateway/test_mcp_jsonrpc_e2e.py -q` | ⚠️ 8 passed, 1 failed (pre-existing baseline blocker, unrelated — see below), 1 deselected |
| Source assertion: `json.loads(line)` present, no try/except | ✅ L80, no `except Exception` |
| Source assertion: `isinstance(msg, dict)` present | ✅ L81 |
| Source assertion: `@pytest.mark.slow` preserved | ✅ L8 |

## Deviations from Plan

None — plan executed exactly as written.

## Deferred / Baseline Blockers

- `tests/gateway/test_mcp_jsonrpc_e2e.py::test_tools_list_matches_registered_surface`
  fails with `pyo3_runtime.PanicException: assertion 'left == right' failed:
  _native::Parser is unsendable, but sent to another thread` originating in
  `src/atelier/infra/tree_sitter/tags.py` (tree-sitter parser sent across threads).
  This is a pre-existing tree-sitter/pyo3 threading issue in unrelated source code,
  **not** a stdout-framing regression and **not** Phase 24 scope. The framing-relevant
  e2e assertions (including the strict per-line parse analog at L306) pass.
- The working tree is dirty with ~180 unrelated changed/deleted paths (documented in
  24-RESEARCH.md "Known Baseline"). Only the Phase 24 hunk in
  `tests/gateway/test_mcp_stdio_smoke.py` was staged/committed; no unrelated hunks were
  touched.

## Self-Check: PASSED

- `tests/gateway/test_mcp_stdio_smoke.py` exists and contains the strict parse. ✅
