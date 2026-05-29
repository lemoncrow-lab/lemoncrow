---
phase: 24-stdout-to-logging
reviewed: 2026-05-29
status: passed
reviewer: gsd-code-reviewer
---

# Phase 24: Stdout to Logging Review

**Verdict:** PASS after LOW follow-ups.

The review found no HIGH or MEDIUM issues. Three LOW findings were addressed in
commit `5f5c607`:

| Finding | Resolution |
| --- | --- |
| `opencode.py` still used `_traceback.print_exc()` on error paths | Replaced both call sites with `logger.exception(...)` and removed the now-unused traceback import. |
| CLI import-progress logger could duplicate via ancestor/root handlers | Set `progress_logger.propagate = False` when installing the scoped stderr handler. |
| MCP stdio smoke malformed JSON failure could be more readable | `JSONDecodeError` now raises `AssertionError` with the raw offending stdout line while preserving strict failure behavior. |

Post-fix checks:

- `uv run ruff check src/atelier/gateway/hosts/session_parsers/opencode.py src/atelier/gateway/cli/app.py tests/gateway/test_mcp_stdio_smoke.py`
- `uv run pytest tests/gateway/test_cli_import_progress.py tests/gateway/test_mcp_stdio_smoke.py -m "" -q`
- `uv run ruff check src --select T20`
- `uv run ruff check src`

All post-fix checks passed.
