---
phase: 23-silent-exception-audit
reviewed: 2026-05-29T20:40:00Z
depth: standard
files_reviewed: 4
files_reviewed_list:
  - src/atelier/gateway/adapters/mcp_server.py
  - src/benchmarks/swe/compact_bench.py
  - src/benchmarks/swe/routing_bench.py
  - src/benchmarks/swe/routing_quality_bench.py
findings:
  critical: 0
  warning: 0
  info: 1
  total: 1
status: issues_found
---

# Phase 23: Code Review Report (Re-review)

**Reviewed:** 2026-05-29T20:40:00Z
**Depth:** standard (re-review after `fc52a9b fix(23): preserve malformed json fail-open paths`)
**Files Reviewed:** 4
**Status:** issues_found (1 Info, non-blocking)

## Summary

This is a targeted re-review of fix commit `fc52a9b`, which addressed the two warnings
(WR-01, WR-02) raised in the prior review. **Both warnings are fully resolved.** No new
blockers or warnings were introduced by the fix. The only remaining item is the previously
filed IN-01 (theoretical, Info), which the fix commit intentionally left untouched.

**Verification performed during this review:**
- Read all three MCP session-file readers and all three benchmark transcript parsers in context.
- `uv run ruff check src --select BLE001` → **All checks passed!** (exit 0)
- `uv run ruff check src` (full) → **All checks passed!** (exit 0)

### WR-01 — RESOLVED
All three MCP readers in `mcp_server.py` now guard the decoded payload with
`isinstance(data, dict)` before calling `.get(...)`:
- `_get_claude_session_id` (line 661)
- `_get_mcp_model` (line 691)
- `_get_host_session_sidecar_path` (line 714)

A valid-but-non-object JSON sidecar (`null`, number, string, list) now falls through the
guard and the existing fail-open fallback runs (`_get_product_session_id()` / cached model /
env-based sidecar). No `AttributeError` can escape the narrowed `(OSError, json.JSONDecodeError)`
tuple from these `.get()` calls. Correct fix.

### WR-02 — RESOLVED
All three benchmark parsers now skip non-dict payloads per-line rather than aborting the file:
- `compact_bench.py:_parse_session` — guards `ev` (line 144), `msg` (154), `usage` (158).
- `routing_bench.py:_parse_session_routing` — guards `ev` (209), `msg` (218), `usage` (222).
- `routing_quality_bench.py:_parse_events` — guards `ev` (290), assistant `msg`/`usage`
  (298/302), and user `msg` (339).

Each non-dict case logs at debug and `continue`s to the next line, preserving partial results
and the per-line resilience these tolerant parsers were written for. The guards are placed
before every subsequent `.get(...)` call, so no `AttributeError` reaches the outer narrowed
tuple. Correct fix.

## Info

### IN-01: `import subprocess as _subprocess` placed inside the guarded try block (carried over, still open)

**File:** `src/atelier/gateway/adapters/mcp_server.py:5439`
**Issue:** In `main()`, `import subprocess as _subprocess` is inside the `try`, while the
`except (OSError, _subprocess.SubprocessError)` clause (line 5449) references the imported
name. If the import itself raised, evaluating the except clause would raise `NameError`,
masking the original error. `subprocess` is stdlib and always importable, so this remains
theoretical only. The analogous block in `cli/app.py` imports outside the `try`.
**Fix:** Move the import above the `try` for consistency:
```python
import subprocess as _subprocess
if not any(os.environ.get(v) for v in _HOST_WORKSPACE_VARS):
    try:
        _git_result = _subprocess.run(...)
```
Non-blocking; optional cleanup.

---

_Reviewed: 2026-05-29T20:40:00Z_
_Reviewer: the agent (gsd-code-reviewer)_
_Depth: standard (re-review)_
