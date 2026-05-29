# Phase 23 Validation Plan: Silent Exception Audit

## Scope Decision

Phase 23 treats the 28 `except Exception: pass` sites as required scope, plus the 3
`except Exception: continue` sites that must be narrowed to remove BLE001 ignores from the
fully cleanable benchmark files. The remaining silent `continue` sites are out of scope for
this surgical phase unless they are in a file already being fixed and can be made observable
without expanding the refactor.

## Requirement Gates

| Requirement | Validation |
|-------------|------------|
| QBL-EXC-01 | Preserve the fresh inventory in `23-RESEARCH.md`; executor re-runs the inventory before editing and records the final count in `23-01-SUMMARY.md`. |
| QBL-EXC-02 | No in-scope `except Exception: pass` sites remain; intentional best-effort suppressions log through `logging` with `exc_info=True` and a local rationale. |
| QBL-EXC-03 | BLE001 per-file ignores are removed for the 8 files identified as fully cleanable; `uv run ruff check src --select BLE001` stays green after deletions. |
| QBL-EXC-04 | Gateway/MCP surfaces touched by `mcp_server.py` changes pass focused MCP tests and no stdout `print()` is introduced. |

## Focused Commands

```bash
uv run ruff check src --select BLE001
uv run ruff check src
uv run pytest tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_stdio_smoke.py -q
uv run pytest tests/gateway/test_sdk_middleware.py tests/infra/test_letta_adapter_fallback.py tests/infra/test_memory_backend_selection.py -q
```

## Optional Regression

If cheap while touching `core/environment.py`, extend
`tests/infra/test_memory_backend_selection.py` with an invalid-config case that asserts the
fallback remains defaulted and emits a warning with exception information.

## Known Baseline

Broad repository gates can still expose unrelated pre-existing issues from the active dirty
checkout. Phase verification should record those separately and must not change unrelated
files just to make broad gates pass.
