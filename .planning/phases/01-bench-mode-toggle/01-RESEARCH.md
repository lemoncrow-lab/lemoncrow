# Phase 1 Research: Bench-Mode Toggle

**Phase:** 1 — Bench-Mode Toggle
**Written:** 2026-05-28
**Status:** RESEARCH COMPLETE

---

## Executive Summary

Phase 1 inserts a thin `ATELIER_BENCH_MODE` env-var toggle that short-circuits four Atelier capabilities (router, compactor, memory reads, MCP tool visibility) and guarantees subprocess-level isolation via separate `ATELIER_ROOT` per arm. No existing bench-mode infrastructure exists — this is greenfield. The codebase architecture makes this straightforward: each capability is a class with a single entry method, and `mcp_server.py` already has a `_tool_visible_to_llm` predicate that controls which tools appear in `tools/list`.

---

## Key Findings

### 1. Existing Infrastructure: None

`grep -r "ATELIER_BENCH_MODE"` hits only `src/benchmarks/swe/savings_replay.py` — unrelated to Phase 1. No bench-mode singleton, no passthrough logic, no existing `src/atelier/bench/` package. Start from scratch.

### 2. Module-Level Singletons (leakage risk)

`mcp_server.py` lines 153–168 declare:
```python
_current_ledger: RunLedger | None = None
_realtime_ctx: RealtimeContextManager | None = None
_product_session_id: str | None = None
# ... more globals
```

These are initialized lazily from `_atelier_root()` (line 307), which reads `ATELIER_ROOT` env var at call time:
```python
def _atelier_root() -> Path:
    return Path(os.environ.get("ATELIER_ROOT", str(default_store_root())))
```

**Implication:** Bench mode runs as *subprocesses* (each `claude -p` invocation is a fresh process), not in-process. The singletons are not an in-process contamination risk between arms. The contamination risk is at the **filesystem level** (`ATELIER_ROOT` directory). Separate `ATELIER_ROOT` per arm (MODE-06) is the correct fix.

### 3. Router to Patch: `cross_vendor_routing/router.py`

**File:** `src/atelier/core/capabilities/cross_vendor_routing/router.py`
**Class:** `CrossVendorRouter` (line 58)
**Method to guard:** `recommend()` (line 75) — returns `CrossVendorRecommendation`

The router selects tiers ("cheap"/"high") and downgrades models. In bench-off mode, it must return the **requested model as-is** without tier manipulation.

There is also `src/atelier/core/capabilities/model_routing/router.py` — a separate, simpler router. Both may need guards.

### 4. Compactor to Patch: `context_compression/capability.py`

**File:** `src/atelier/core/capabilities/context_compression/capability.py`
**Class:** `ContextCompressionCapability`
**Method to guard:** `compress_with_provenance()` (line ~34) — returns `CompressionResult`

`CompressionResult` is in `context_compression/models.py`. Need a `CompressionResult.passthrough(ledger)` classmethod or equivalent that returns an unmodified result.

### 5. Memory Adapters to Patch: `cross_vendor_memory/`

**Files:**
- `src/atelier/core/capabilities/cross_vendor_memory/claude_adapter.py` — `ClaudeAdapter` (line 140), read path at line 39 already returns `[]` in some error path
- `src/atelier/core/capabilities/cross_vendor_memory/codex_adapter.py` — `CodexAdapter` (line 125), similar pattern
- `src/atelier/core/capabilities/cross_vendor_memory/gemini_adapter.py` — `GeminiAdapter` (line 142)
- `src/atelier/core/capabilities/cross_vendor_memory/registry.py` — `MemoryRegistry._load()` (line 36) aggregates all adapters

**Best insertion point:** `MemoryRegistry._load()` — single guard catches all adapters:
```python
def _load(self) -> list[MemoryFact]:
    if bench.is_off():
        return []
    ...
```

### 6. MCP Tool Visibility: `mcp_server.py` + `environment.py`

Tools are listed at line 5117:
```python
for n, s in TOOLS.items()
    if _tool_visible_to_llm(n, s)
```

`_tool_visible_to_llm` (line 86) delegates to `environment.mcp_tool_visible_to_llm(tool_name)`. The environment function (lines 93–96) checks `STABLE_LLM_TOOLS` or `is_dev_mode()`.

**Bench-mode gating strategy:** Gate at the `_tool_visible_to_llm` level in `environment.py`. When `bench.is_off()`, return `False` for all non-passthrough tools (effectively: off-arm agent can only use its native tools, not Atelier MCP tools). This is the correct place — it's already the single choke point.

**`ATELIER_DEV_MODE` risk:** `is_dev_mode()` reads `ATELIER_DEV_MODE` from `os.environ`. If the benchmark subprocess inherits `ATELIER_DEV_MODE=1`, it would bypass `STABLE_LLM_TOOLS` check. Bench-mode must **override** dev mode check: bench-off takes precedence regardless of `ATELIER_DEV_MODE`.

### 7. CLI Entry Point

**File:** `src/atelier/gateway/cli/app.py`
**Function:** `main()` at line 8373
**CLI group:** `cli` at line 1147 — `@click.group`

`bench.bootstrap()` should be called as the first line of `main()` (before Click parses commands) so mode is frozen before any lazy singleton initialization.

### 8. `ATELIER_DEV_MODE` vs `ATELIER_BENCH_MODE` Precedence

`environment.py` `is_dev_mode()` reads `ATELIER_DEV_MODE`. It currently gates all tool visibility and skill availability. Bench mode must **not** be defeatable by `ATELIER_DEV_MODE`. Design rule: `bench.is_off()` short-circuits before any dev-mode check in the affected code paths.

### 9. Telemetry Tagging

Telemetry emitted from `mcp_server.py` (e.g., `session_telemetry.py`) should include `bench_mode=on|off`. Tagging point: add to the trace/event payloads when `bench.bootstrap()` is called. No separate telemetry file changes needed — tag at the `Trace` model level in `core/foundation/models.py` or inject via run ledger metadata.

### 10. Existing Test Patterns

From `tests/core/capabilities/` and `tests/gateway/`:
- Tests use `monkeypatch.setenv("ENV_VAR", "value")` for env-var-gated behavior
- Module-level singletons in `mcp_server.py` are reset between tests via `monkeypatch` or fixture teardown (see `tests/gateway/test_mcp_tool_handlers.py` — resets `_remote_client`)
- Unit tests for capabilities test the class directly without importing mcp_server
- Pattern: `from atelier.core.capabilities.X import XCapability; cap = XCapability(); result = cap.method()`

---

## Proposed `src/atelier/bench/` Package Structure

```
src/atelier/bench/
├── __init__.py          # exports: bootstrap, is_off, BenchMode
└── mode.py              # BenchMode enum + singleton
```

```python
# mode.py
import os
from enum import Enum

class BenchMode(str, Enum):
    ON = "on"
    OFF = "off"

_mode: BenchMode | None = None

def bootstrap() -> None:
    global _mode
    raw = os.environ.get("ATELIER_BENCH_MODE", "on").strip().lower()
    _mode = BenchMode.OFF if raw == "off" else BenchMode.ON

def is_off() -> bool:
    if _mode is None:
        bootstrap()  # lazy fallback for processes that don't call main()
    return _mode == BenchMode.OFF

def mode() -> BenchMode:
    if _mode is None:
        bootstrap()
    return _mode  # type: ignore[return-value]
```

**Design note:** Lazy `bootstrap()` in `is_off()` ensures that test code that imports the module without calling `main()` still works correctly.

---

## Files to Create/Modify

| File | Action | Why |
|------|--------|-----|
| `src/atelier/bench/__init__.py` | **Create** | New package |
| `src/atelier/bench/mode.py` | **Create** | Singleton (MODE-01..05) |
| `src/atelier/core/capabilities/cross_vendor_routing/router.py` | **Modify** | Passthrough guard in `recommend()` (MODE-01) |
| `src/atelier/core/capabilities/model_routing/router.py` | **Modify** | Passthrough guard (MODE-01) |
| `src/atelier/core/capabilities/context_compression/capability.py` | **Modify** | Skip compression when off (MODE-02) |
| `src/atelier/core/capabilities/context_compression/models.py` | **Modify** | Add `CompressionResult.passthrough()` factory (MODE-02) |
| `src/atelier/core/capabilities/cross_vendor_memory/registry.py` | **Modify** | `_load()` returns `[]` when off (MODE-03) |
| `src/atelier/core/environment.py` | **Modify** | `mcp_tool_visible_to_llm` returns False when bench off (MODE-04); bench overrides dev mode |
| `src/atelier/gateway/cli/app.py` | **Modify** | Call `bench.bootstrap()` first in `main()` + telemetry tag (MODE-05) |
| `tests/core/test_bench_mode.py` | **Create** | Unit tests (MODE-07): router passthrough, compactor skip, memory empty, mcp hidden |
| `tests/core/test_bench_mode_integration.py` | **Create** | Integration test (MODE-08): same prompt → different token counts |

---

## Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| `_mode` singleton persists across test cases | Medium | Reset `_mode = None` in test teardown via monkeypatch |
| `ATELIER_DEV_MODE=1` in env overrides bench-off tool gating | High | Bench check runs before dev-mode check in `mcp_tool_visible_to_llm` |
| `CompressionResult.passthrough()` not returning correct shape | Medium | Inspect existing `CompressionResult` fields; passthrough = keep all events, no drops |
| Lazy `bootstrap()` in `is_off()` means race condition in threaded code | Low | `_mode` write is atomic (Python GIL); bootstrap reads env once, acceptable |
| Integration test requires real Claude invocation (expensive) | Medium | Mock subprocess for unit test; integration test is optional/manual |

---

## Validation Architecture

### Test Coverage

| Requirement | Test | Type |
|-------------|------|------|
| MODE-01 (router passthrough) | `test_bench_mode_off_passthrough_router` | Unit |
| MODE-02 (compactor skip) | `test_bench_mode_off_passthrough_compactor` | Unit |
| MODE-03 (memory empty) | `test_bench_mode_off_memory_returns_empty` | Unit |
| MODE-04 (MCP tools hidden) | `test_bench_mode_off_mcp_tools_invisible` | Unit |
| MODE-05 (bootstrap reads env once) | `test_bench_bootstrap_reads_env_once` | Unit |
| MODE-06 (separate ATELIER_ROOT) | `test_bench_arm_uses_separate_root` | Unit |
| MODE-07 (unit test coverage) | All above | Unit |
| MODE-08 (measurable token difference) | `test_bench_on_vs_off_token_counts_differ` | Integration |

### Verification Steps

1. `ATELIER_BENCH_MODE=off uv run atelier --version` runs clean (no router/compactor invocation)
2. `uv run pytest tests/core/test_bench_mode.py -q` — all unit tests pass
3. `uv run pytest tests/core/test_bench_mode_integration.py -q` — integration test shows `on_tokens > off_tokens`
4. `make lint && make typecheck` — no new violations
5. Existing test suite: `uv run pytest -q -m "not slow"` — no regressions

---

## RESEARCH COMPLETE
