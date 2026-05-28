# Phase 1: Bench-Mode Toggle — Pattern Map

**Mapped:** 2025-01-28
**Files analyzed:** 10 new/modified files
**Analogs found:** 9 / 10

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `src/atelier/bench/mode.py` | utility/singleton | request-response | `src/atelier/core/environment.py` | exact |
| `src/atelier/bench/__init__.py` | config/package | — | `src/atelier/core/capabilities/cross_vendor_routing/__init__.py` | structural |
| `src/atelier/core/capabilities/cross_vendor_routing/router.py` (guard) | service/capability | request-response | `src/atelier/core/environment.py` `mcp_tool_visible_to_llm()` | role-match |
| `src/atelier/core/capabilities/model_routing/router.py` (guard) | service/capability | request-response | `src/atelier/core/environment.py` `mcp_tool_visible_to_llm()` | role-match |
| `src/atelier/core/capabilities/context_compression/capability.py` (guard) | service/capability | transform | `src/atelier/core/capabilities/cross_vendor_memory/registry.py` `_load()` | role-match |
| `src/atelier/core/capabilities/context_compression/models.py` (factory) | model | transform | `src/atelier/core/capabilities/context_compression/models.py` `CompressionResult` | exact |
| `src/atelier/core/capabilities/cross_vendor_memory/registry.py` (guard) | service/registry | CRUD | `src/atelier/core/capabilities/cross_vendor_memory/registry.py` | exact |
| `src/atelier/core/environment.py` (guard) | utility/policy | request-response | `src/atelier/core/environment.py` | exact |
| `src/atelier/gateway/cli/app.py` (bootstrap) | CLI entrypoint | request-response | `src/atelier/gateway/cli/app.py` `main()` | exact |
| `tests/core/test_bench_mode.py` | test | — | `tests/core/test_environment_policy.py` | exact |

---

## Pattern Assignments

---

### `src/atelier/bench/mode.py` (utility/singleton)

**Analog:** `src/atelier/core/environment.py`

This is the closest real analog — it is the codebase's only other env-var singleton. Copy its
conventions exactly: `from __future__ import annotations`, `os.environ` access with `.strip().lower()`,
a `frozenset`-style constant, and injectable `env: Mapping[str, str] | None = None` on any helpers.

**Imports pattern** (`environment.py` lines 1–18):
```python
from __future__ import annotations

import os
from collections.abc import Mapping

# (no pathlib/tomllib needed for mode.py)
```

**Env-var constant + reader pattern** (`environment.py` lines 19–22, 69–78):
```python
DEV_MODE_ENV_VAR = "ATELIER_DEV_MODE"
TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})

def bool_env(name: str, default: bool = False, env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    raw = values.get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in TRUE_ENV_VALUES

def is_dev_mode(env: Mapping[str, str] | None = None) -> bool:
    return bool_env(DEV_MODE_ENV_VAR, False, env)
```

**New singleton pattern for `mode.py`** — adapt the above with a module-level `_mode` variable:
```python
BENCH_MODE_ENV_VAR = "ATELIER_BENCH_MODE"

_mode: BenchMode | None = None   # module-level sentinel; None = not bootstrapped

def bootstrap() -> None:
    global _mode
    raw = os.environ.get(BENCH_MODE_ENV_VAR, "on").strip().lower()
    _mode = BenchMode.OFF if raw == "off" else BenchMode.ON

def is_off() -> bool:
    if _mode is None:
        bootstrap()           # lazy fallback — safe under Python GIL
    return _mode == BenchMode.OFF
```

**`str, Enum` pattern** — use `str, Enum` so values serialize cleanly in logs/telemetry. No existing
enum of this type exists in `environment.py`, but RESEARCH.md proposes it and it is idiomatic Python.

---

### `src/atelier/bench/__init__.py` (package init)

**Analog:** `src/atelier/core/capabilities/cross_vendor_routing/__init__.py`

**Pattern** — re-export the public API from the leaf module. Keep `__init__.py` thin:
```python
from atelier.bench.mode import BenchMode, bootstrap, is_off, mode

__all__ = ["BenchMode", "bootstrap", "is_off", "mode"]
```

---

### `src/atelier/core/capabilities/cross_vendor_routing/router.py` — guard in `recommend()` (lines 75–140)

**Analog:** `src/atelier/core/environment.py` `mcp_tool_visible_to_llm()` (lines 93–96)

The `mcp_tool_visible_to_llm` function demonstrates the canonical early-return-before-policy pattern:
check the override condition first, then fall through to normal logic.

**Early-return guard pattern** (`environment.py` lines 93–96):
```python
def mcp_tool_visible_to_llm(tool_name: str) -> bool:
    if is_dev_mode():          # ← condition checked before any other logic
        return True
    return tool_name in STABLE_LLM_TOOLS
```

**Apply to `CrossVendorRouter.recommend()`** (`router.py` lines 75–82). Insert at the top of the
method body, before line 83 (`enabled = ...`):
```python
def recommend(
    self,
    *,
    tool_name: str,
    task_text: str,
    session_state: Mapping[str, Any] | None = None,
    actual_vendor: str | None = None,
) -> CrossVendorRecommendation:
    from atelier.bench.mode import is_off as bench_is_off  # lazy import avoids circular dep
    if bench_is_off():
        # Bench-off: return the actual vendor/model as-is without tier manipulation
        # (caller passes actual_vendor; fall back to first enabled vendor if absent)
        passthrough_vendor = actual_vendor or (self._config.enabled_vendors[0] if self._config.enabled_vendors else "unknown")
        return CrossVendorRecommendation(
            vendor=passthrough_vendor,
            model=session_state.get("model", "auto") if session_state else "auto",
            tier="high",
            estimated_cost_usd=0.0,
            reasons=("bench_mode=off: passthrough",),
        )
    # ... existing recommend() body follows unchanged
```

**Why lazy import:** `router.py` uses `from __future__ import annotations` and has no circular deps
today. Using a lazy `import` inside the method body (as done in `capability.py` lines 167–173 for
`SqliteMemoryStore`) is the established pattern when a new dep would create a potential import cycle.

---

### `src/atelier/core/capabilities/model_routing/router.py` — guard in `score()` / `recommend()`

**Analog:** Same pattern as `cross_vendor_routing/router.py` guard above.

The `ModelRouter` is simpler — it returns a `RoutingDecision` dataclass. Same early-return strategy:
insert `if bench_is_off(): return <passthrough RoutingDecision>` at the top of the scoring method
before any tier-selection logic. The exact field names should be verified against `ModelRouter.score()`
return type.

---

### `src/atelier/core/capabilities/context_compression/capability.py` — guard in `compress_with_provenance()` (lines 34–111)

**Analog 1:** `src/atelier/core/capabilities/cross_vendor_memory/registry.py` `_load()` (lines 36–43) — demonstrates the "cache sentinel + early return empty" pattern.

**Analog 2:** `src/atelier/core/environment.py` `mcp_tool_visible_to_llm()` — early return before processing.

**Cache/sentinel guard from `registry.py` lines 36–43:**
```python
def _load(self) -> list[MemoryFact]:
    if self._cache is None:       # ← sentinel check
        facts: list[MemoryFact] = []
        for adapter in self._adapters:
            if adapter.is_available():
                facts.extend(adapter.list_facts())
        self._cache = facts
    return self._cache
```

**Apply to `compress_with_provenance()`** — insert at line 50 (before `raw_events = []`):
```python
def compress_with_provenance(
    self,
    ledger: RunLedger,
    *,
    token_budget: int = 8000,
    task: str = "",
) -> CompressionResult:
    from atelier.bench.mode import is_off as bench_is_off
    if bench_is_off():
        return CompressionResult.passthrough(ledger)   # new classmethod — see models.py below
    # ... existing body unchanged from line 50
```

---

### `src/atelier/core/capabilities/context_compression/models.py` — `CompressionResult.passthrough()` factory

**Analog:** `src/atelier/core/capabilities/context_compression/models.py` `CompressionResult` (lines 29–48)

**`CompressionResult` shape** (lines 30–38):
```python
@dataclass
class CompressionResult:
    chars_before: int
    chars_after: int
    reduction_pct: float
    preserved_facts: list[str]
    dropped: list[DroppedContext]
    token_savings: int = 0
```

**`to_dict()` pattern** (lines 40–48) — existing serialization method shows the contract:
```python
def to_dict(self) -> dict[str, Any]:
    return {
        "chars_before": self.chars_before,
        "chars_after": self.chars_after,
        "reduction_pct": self.reduction_pct,
        "preserved_facts": self.preserved_facts,
        "dropped": [d.to_dict() for d in self.dropped],
        "token_savings": self.token_savings,
    }
```

**New `passthrough()` classmethod to add** — a passthrough result signals "no compression happened":
```python
@classmethod
def passthrough(cls, ledger: Any) -> "CompressionResult":
    """Return a zero-compression result for bench-off / passthrough mode."""
    import contextlib
    raw_events: list[Any] = []
    with contextlib.suppress(Exception):
        raw_events = list(getattr(ledger, "events", []) or [])
    chars = sum(
        len(str(getattr(ev, "summary", ev.get("summary", "") if isinstance(ev, dict) else "")))
        for ev in raw_events
    )
    return cls(
        chars_before=chars,
        chars_after=chars,
        reduction_pct=0.0,
        preserved_facts=[],
        dropped=[],
        token_savings=0,
    )
```

**Key constraint:** `dropped=[]` and `reduction_pct=0.0` are the sentinel values that callers
(e.g., `context_report()` → `to_dict()`) will see to indicate no compression occurred. This matches
the "no work done" contract implied by the `to_dict()` serialization shape.

---

### `src/atelier/core/capabilities/cross_vendor_memory/registry.py` — guard in `_load()` (lines 36–43)

**Analog:** `registry.py` itself — `_load()` already has the cache-sentinel pattern. Add a second
condition before the cache check.

**Existing `_load()` body** (lines 36–43):
```python
def _load(self) -> list[MemoryFact]:
    if self._cache is None:
        facts: list[MemoryFact] = []
        for adapter in self._adapters:
            if adapter.is_available():
                facts.extend(adapter.list_facts())
        self._cache = facts
    return self._cache
```

**Modified version:**
```python
def _load(self) -> list[MemoryFact]:
    from atelier.bench.mode import is_off as bench_is_off
    if bench_is_off():
        return []          # bench-off: no memory reads, no caching
    if self._cache is None:
        facts: list[MemoryFact] = []
        for adapter in self._adapters:
            if adapter.is_available():
                facts.extend(adapter.list_facts())
        self._cache = facts
    return self._cache
```

**Note:** The bench guard returns `[]` without setting `self._cache` — this is intentional so that
when bench mode is re-enabled (in tests via monkeypatch), the registry can repopulate normally.

---

### `src/atelier/core/environment.py` — guard in `mcp_tool_visible_to_llm()` (lines 93–96)

**Analog:** `environment.py` itself — `mcp_tool_visible_to_llm()` already has the is_dev_mode early-return pattern.

**Existing function** (lines 93–96):
```python
def mcp_tool_visible_to_llm(tool_name: str) -> bool:
    if is_dev_mode():
        return True
    return tool_name in STABLE_LLM_TOOLS
```

**Modified version — bench check runs BEFORE dev-mode check:**
```python
def mcp_tool_visible_to_llm(tool_name: str) -> bool:
    from atelier.bench.mode import is_off as bench_is_off  # lazy to avoid circular import
    if bench_is_off():
        return False   # bench-off: hide ALL Atelier MCP tools from LLM
    if is_dev_mode():
        return True
    return tool_name in STABLE_LLM_TOOLS
```

**Critical ordering:** `bench_is_off()` guard **must** precede `is_dev_mode()` so that
`ATELIER_DEV_MODE=1` in the subprocess environment cannot defeat bench-off tool hiding.
This is the same override precedence pattern as `is_dev_mode()` overriding `STABLE_LLM_TOOLS`.

---

### `src/atelier/gateway/cli/app.py` — `bench.bootstrap()` call in `main()` (lines 8373–8400)

**Analog:** `src/atelier/gateway/cli/app.py` `main()` (lines 8373–8400)

**Existing `main()` preamble** (lines 8373–8396):
```python
def main() -> None:
    command_name = _cli_command_name(sys.argv[1:])
    session_id, started_at = _begin_cli_telemetry(command_name)
    old_handlers: dict[int, Any] = {}

    def _handler(signum: int, frame: Any) -> None:
        _emit_cli_interrupted(...)
        ...
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _handler)

    try:
        try:
            cli(obj={...})
```

**Bootstrap insertion point** — insert as the very first line of `main()`, before all telemetry
and signal registration:
```python
def main() -> None:
    import atelier.bench as bench   # or: from atelier.bench.mode import bootstrap as bench_bootstrap
    bench.bootstrap()               # freeze ATELIER_BENCH_MODE before any lazy singleton init
    command_name = _cli_command_name(sys.argv[1:])
    session_id, started_at = _begin_cli_telemetry(command_name)
    # ... rest of main() unchanged
```

**Why first line:** `_begin_cli_telemetry()` and signal registration read no singletons, but
`cli()` → any command handler → `_atelier_root()` could trigger lazy initialization of
`_current_ledger` (line 153–216 in mcp_server.py). `bench.bootstrap()` must run before that chain.

---

### `tests/core/test_bench_mode.py` (new test file)

**Analog:** `tests/core/test_environment_policy.py`

This is the best test analog — same structure, same subject matter (env-var policy functions),
same test style (no fixtures, direct function calls, `monkeypatch.setenv` / `monkeypatch.delenv`).

**Full test file structure from `test_environment_policy.py`** (lines 1–31):
```python
from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.environment import resolve_memory_backend


def test_resolve_memory_backend_defaults_to_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_MEMORY_BACKEND", raising=False)
    assert resolve_memory_backend(root=tmp_path) == "sqlite"


def test_resolve_memory_backend_prefers_env_over_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_MEMORY_BACKEND", "openmemory")
    assert resolve_memory_backend(root=tmp_path) == "openmemory"
```

**Critical addition for singleton tests** — must reset `_mode` between test cases. The conftest
`_isolate_workspace_env` fixture uses `monkeypatch.delenv` / `monkeypatch.setenv`; add an analogous
bench-mode reset fixture:
```python
# In tests/core/test_bench_mode.py
import pytest
import atelier.bench.mode as _bench_mode

@pytest.fixture(autouse=True)
def _reset_bench_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the bench mode singleton before each test."""
    monkeypatch.setattr(_bench_mode, "_mode", None)
    monkeypatch.delenv("ATELIER_BENCH_MODE", raising=False)
```

**`monkeypatch.setattr` on module-level var** — this is the correct mechanism for resetting
module-level singletons. The `mcp_server.py` test file (`test_mcp_memory_tools.py` line 153)
uses `monkeypatch.setattr(mcp_server, "_memory_store", lambda: DownStore())` for the same purpose.

**Env-var set/delete pattern** (`test_environment_policy.py` lines 10–18):
```python
def test_X(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_BENCH_MODE", raising=False)   # ensure no env var
    # or:
    monkeypatch.setenv("ATELIER_BENCH_MODE", "off")            # set specific value
    from atelier.bench.mode import bootstrap, is_off
    bootstrap()
    assert is_off() is True
```

---

## Shared Patterns

### 1. Lazy Import Guard (Anti-Circular-Import)

**Source:** `src/atelier/core/capabilities/context_compression/capability.py` lines 167–173 (imports
`SqliteMemoryStore` lazily inside a method body)
**Apply to:** All guard insertions in `router.py`, `capability.py`, `registry.py`, `environment.py`

```python
from atelier.bench.mode import is_off as bench_is_off  # inside method body, not module level
```

Use lazy imports inside method/function bodies for the `bench.mode` import everywhere it is added.
This prevents circular import issues since `bench.mode` imports only `os` and `enum`.

---

### 2. Env-Var Read-at-Call-Time Pattern

**Source:** `src/atelier/core/environment.py` `bool_env()` lines 69–74
**Apply to:** `bench/mode.py` `bootstrap()`

```python
def bool_env(name: str, default: bool = False, env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env   # injectable for testing
    raw = values.get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in TRUE_ENV_VALUES
```

The injectable `env: Mapping[str, str] | None = None` parameter pattern allows unit tests to pass
a dict without mutating `os.environ`. `bench/mode.py`'s `bootstrap()` should support this too for
testability — see RESEARCH.md proposed signature.

---

### 3. Module-Level Singleton with Lazy Init

**Source:** `src/atelier/gateway/adapters/mcp_server.py` lines 153–216

```python
_current_ledger: RunLedger | None = None   # module-level sentinel

def _ledger(root: Path) -> RunLedger:
    global _current_ledger
    if _current_ledger is None:
        ...
        _current_ledger = RunLedger(root=root, agent=_detect_agent())
    return _current_ledger
```

Same pattern for `bench/mode.py`'s `_mode: BenchMode | None = None`. Initialize lazily on first
call to `is_off()` or `mode()` if `bootstrap()` was not called explicitly.

---

### 4. Test Monkeypatch for Module-Level Singletons

**Source:** `tests/gateway/test_mcp_memory_tools.py` line 153
**Apply to:** `tests/core/test_bench_mode.py`

```python
# Reset a module-level variable between tests:
monkeypatch.setattr(mcp_server, "_REMOTE_TOOLS", frozenset())
# Reset bench mode between tests:
monkeypatch.setattr(bench_mode_module, "_mode", None)
```

---

### 5. `mcp_tool_visible_to_llm` Override Precedence

**Source:** `src/atelier/core/environment.py` lines 93–96 + `mcp_server.py` lines 85–86
**Apply to:** `environment.py` modification

The existing pattern checks `is_dev_mode()` first, then falls through. The bench-mode guard
must be inserted **before** `is_dev_mode()` to ensure bench-off cannot be overridden by
`ATELIER_DEV_MODE=1`. Precedence: `bench_is_off()` > `is_dev_mode()` > `STABLE_LLM_TOOLS`.

---

## `CompressionResult` Passthrough Shape

**Answer to key question 4:** "What does a passthrough result look like?"

Based on `models.py` (lines 29–48):

| Field | Passthrough value | Rationale |
|---|---|---|
| `chars_before` | Sum of chars in ledger events | Preserve actual ledger size for telemetry |
| `chars_after` | Same as `chars_before` | No chars were dropped |
| `reduction_pct` | `0.0` | Zero reduction |
| `preserved_facts` | `[]` | No analysis performed |
| `dropped` | `[]` | Nothing was dropped |
| `token_savings` | `0` | No savings |

Callers of `to_dict()` will see `reduction_pct=0.0` and `dropped=[]` as the passthrough sentinel.
The `context_report()` method (line 219) calls `to_dict()` — its output will reflect no compression.

---

## `recommend()` Call Chain from `mcp_server.py`

**Answer to key question 5:** How `router.py` `recommend()` is currently called from `mcp_server.py`

**Call site** (`mcp_server.py` lines 4960–4975):
```python
def _emit_model_recommendation(tool_name: str, args: dict[str, Any], led: RunLedger) -> dict[str, Any]:
    from atelier.core.capabilities.cross_vendor_routing.advisor import CrossVendorRouteAdvisor
    from atelier.core.capabilities.cross_vendor_routing.router import NoFeasibleRouteError
    ...
    advisor = CrossVendorRouteAdvisor(_atelier_root())
    try:
        recommendation = advisor.recommend(   # ← calls CrossVendorRouteAdvisor.recommend()
            tool_name=tool_name,
            task_text=_task_text_from_args(args),
            session_state=session_state,
        )
```

The actual call goes through `CrossVendorRouteAdvisor` (an adapter in `advisor.py`), which internally
delegates to `CrossVendorRouter.recommend()`. There is also a second call at line 1451 via the same
advisor pattern. The guard in `router.py` will intercept both.

---

## No Analog Found

| File | Role | Data Flow | Reason |
|---|---|---|---|
| `src/atelier/bench/__init__.py` | package init | — | Trivial re-export; use `cross_vendor_routing/__init__.py` as structural template |

---

## Metadata

**Analog search scope:** `src/atelier/`, `tests/core/`, `tests/gateway/`
**Files scanned:** 15 source files, 5 test files
**Key files read in full:** `environment.py`, `cross_vendor_routing/router.py`, `context_compression/capability.py`, `context_compression/models.py`, `cross_vendor_memory/registry.py`, `tests/core/test_environment_policy.py`, `tests/core/capabilities/cross_vendor_memory/test_cross_vendor_memory.py`, `tests/conftest.py`
**Pattern extraction date:** 2025-01-28
