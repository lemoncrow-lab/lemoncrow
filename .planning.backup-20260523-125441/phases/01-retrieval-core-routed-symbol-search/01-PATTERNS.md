# Phase 1: Retrieval Core & Routed Symbol Search - Pattern Map

**Mapped:** 2026-05-18  
**Files analyzed:** 15  
**Analogs found:** 15 / 15

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `src/atelier/core/capabilities/code_context/budget.py` | utility | transform | `src/atelier/core/capabilities/code_context/budget.py` | exact |
| `src/atelier/core/capabilities/code_context/cache.py` | service | CRUD | `src/atelier/core/capabilities/code_context/cache.py` | exact |
| `src/atelier/core/capabilities/code_context/models.py` | model | transform | `src/atelier/core/capabilities/code_context/models.py` | exact |
| `src/atelier/core/capabilities/code_context/engine.py` | service | request-response | `src/atelier/core/capabilities/code_context/engine.py` | exact |
| `src/atelier/gateway/adapters/mcp_server.py` | route | request-response | `src/atelier/gateway/adapters/mcp_server.py` | exact |
| `src/atelier/infra/code_intel/scip/__init__.py` | config | transform | `src/atelier/core/capabilities/code_context/__init__.py` | package-match |
| `src/atelier/infra/code_intel/scip/provider.py` | provider | request-response | `src/atelier/core/capabilities/cross_vendor_memory/base.py` | role-match |
| `src/atelier/infra/code_intel/scip/store.py` | service | CRUD | `src/atelier/core/capabilities/tool_supervision/store.py` | role-match |
| `src/atelier/infra/code_intel/scip/indexer.py` | service | file-I/O | `src/atelier/core/capabilities/semantic_file_memory/indexer.py` | strong-flow-match |
| `tests/core/test_code_context.py` | test | request-response | `tests/core/test_code_context.py` | exact |
| `tests/gateway/test_p0_mcp_surfaces.py` | test | request-response | `tests/gateway/test_p0_mcp_surfaces.py` | exact |
| `tests/gateway/test_mcp_tool_handlers.py` | test | request-response | `tests/gateway/test_mcp_tool_handlers.py` | exact |
| `tests/gateway/test_savings_api.py` | test | event-driven | `tests/gateway/test_savings_api.py` | exact |
| `tests/benchmarks/code_intel/test_symbol_search_bench.py` | test | batch | `tests/infra/test_context_savings_smoke.py` | role-match |
| `docs/agent-os/validation-matrix.md` | config | batch | `docs/agent-os/validation-matrix.md` | exact |

## Pattern Assignments

### `src/atelier/core/capabilities/code_context/budget.py` (utility, transform)

**Analog:** `src/atelier/core/capabilities/code_context/budget.py`

**Imports + token counting** (`budget.py:1-12`)
```python
import json
from typing import Any

from atelier.core.capabilities.repo_map.budget import count_tokens

def _token_count(items: list[dict[str, Any]]) -> int:
    return count_tokens(json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str))
```

**Core packing pattern** (`budget.py:18-74`)  
Keep the current “drop optional keys first, preserve top essentials, then trim rows” flow. New search/snippet packing should follow this exact order.

---

### `src/atelier/core/capabilities/code_context/cache.py` (service, CRUD)

**Analog:** `src/atelier/core/capabilities/code_context/cache.py`

**Canonical keying** (`cache.py:12-14`, `cache.py:82-91`)
```python
def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)

payload = f"{_canonical_json(args)}|{index_version}|{repo_id}|{tool_name}"
return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

**SQLite upsert + LRU eviction** (`cache.py:55-80`, `cache.py:113-129`)  
Reuse this exact `INSERT ... ON CONFLICT` style and size-based eviction loop for any routed symbol cache.

---

### `src/atelier/core/capabilities/code_context/models.py` (model, transform)

**Analog:** `src/atelier/core/capabilities/code_context/models.py`

**Pydantic model shape** (`models.py:7-30`)
```python
from pydantic import BaseModel, ConfigDict

class SymbolRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol_id: str
    repo_id: str
    file_path: str
    ...
    provenance: str = "local"
```

**Planner note:** if Phase 1 adds stable SCIP IDs or routed provenance, extend these models in place instead of adding parallel payload types.

---

### `src/atelier/core/capabilities/code_context/engine.py` (service, request-response)

**Analog:** `src/atelier/core/capabilities/code_context/engine.py`

**Import and dependency shape** (`engine.py:19-34`)
```python
from atelier.core.capabilities.code_context.budget import BudgetPacker
from atelier.core.capabilities.code_context.cache import RetrievalCache
from atelier.core.capabilities.code_context.models import (
    ContextPack,
    ImpactResult,
    IndexStats,
    SymbolRecord,
    TextMatch,
)
from atelier.infra.tree_sitter.tags import detect_language, extract_tags
```

**Index/build pattern** (`engine.py:190-260`)
```python
files = [
    path
    for path in iter_source_files(self.repo_root, include_globs=include_globs)
    if not self._excluded(path, exclude_globs or [])
]
...
conn.execute("DELETE FROM symbol_fts")
conn.execute("DELETE FROM symbols")
conn.execute("DELETE FROM imports")
conn.execute("DELETE FROM files")
...
index_version = self._bump_index_version(conn)
```

**Search wrapper + cache pattern** (`engine.py:278-312`)
```python
cache_args = {
    "query": query,
    "limit": limit,
    "kind": kind,
    "language": language,
    "budget_tokens": budget_tokens,
}
hit, cached = self._cache_get("code.search", cache_args)
if hit and cached is not None:
    return self._mark_cache_hit(cached)
```

**Search ranking pattern** (`engine.py:475-525`)
```python
SELECT s.*, 1.0 / (1.0 + abs(bm25(symbol_fts))) AS score
FROM symbol_fts
JOIN symbols s ON s.symbol_id = symbol_fts.symbol_id
WHERE symbol_fts MATCH ? AND s.repo_id = ?{where_extra}
ORDER BY
    CASE
        WHEN lower(s.symbol_name) = ? THEN 0
        WHEN lower(s.qualified_name) = ? THEN 1
        ELSE 2
    END,
    bm25(symbol_fts), s.file_path, s.start_line
LIMIT ?
```

**Context packing pattern** (`engine.py:617-682`)  
Preserve the existing outline-first assembly: repo map, import neighbors, then exact symbol source blocks until the token budget is hit.

**Shared helpers to copy** (`engine.py:884-897`, `engine.py:1200-1234`, `engine.py:1248-1304`)
- path normalization + path-escape denial
- cache reads keyed by current `index_version`
- `_mark_cache_hit()` setting `cache_hit=True` and `provenance="cached"`

---

### `src/atelier/gateway/adapters/mcp_server.py` (route, request-response)

**Analog:** `src/atelier/gateway/adapters/mcp_server.py`

**Gateway wrapper pattern** (`mcp_server.py:1905-1993`)
```python
def _code_context_engine(repo_root: str = ".") -> Any:
    ...
    return CodeContextEngine(resolved)

@mcp_tool(name="code", is_dev=True)
def tool_code(...):
    engine = _code_context_engine(repo_root)
    if op == "search":
        if not query:
            raise ValueError("query is required for code search")
        return cast(dict[str, Any], engine.tool_search(...))
```

**Do not add a new top-level MCP tool.** Extend `tool_code` and keep the existing `op` dispatch.

**Search-surface gating pattern** (`mcp_server.py:2022-2097`)  
`tool_smart_search()` already routes between native search selectors and smart-search mode. Use this as the model for adding `snippet`, `snippet_lines`, `file_glob`, or `scope` without widening the public tool list.

**Savings metadata pattern** (`mcp_server.py:2338-2438`)
```python
if "cache_hit" in result and "cache_hit" not in savings_metadata:
    savings_metadata["cache_hit"] = bool(result.get("cache_hit"))
if isinstance(result.get("provenance"), str):
    savings_metadata.setdefault("provenance", str(result["provenance"]))
...
recorder.record(
    session_id=led.session_id,
    ...
    output_tokens=actual_output_tokens,
    naive_input_tokens=actual_output_tokens + tokens_saved,
    lever_savings=lever_savings,
    tool_calls=1,
)
```

---

### `src/atelier/infra/code_intel/scip/__init__.py` (config, transform)

**Analog:** `src/atelier/core/capabilities/code_context/__init__.py`

**Package export pattern** (`code_context/__init__.py:1-23`)
```python
"""Atelier-native code context engine."""

from atelier.core.capabilities.code_context.budget import BudgetPacker
from atelier.core.capabilities.code_context.cache import RetrievalCache
from atelier.core.capabilities.code_context.engine import CodeContextEngine
...
__all__ = [...]
```

**Planner note:** keep the new package init small: docstring, direct imports, explicit `__all__`.

---

### `src/atelier/infra/code_intel/scip/provider.py` (provider, request-response)

**Analog:** `src/atelier/core/capabilities/cross_vendor_memory/base.py`

**Protocol pattern** (`cross_vendor_memory/base.py:30-48`)
```python
@runtime_checkable
class MemoryAdapter(Protocol):
    vendor: str

    def is_available(self) -> bool: ...
    def list_facts(self) -> list[MemoryFact]: ...
    def source_paths(self) -> list[Path]: ...
```

**Stable ID helper pattern** (`cross_vendor_memory/base.py:49-58`)
```python
def _fact_id(vendor: str, content: str) -> str:
    import hashlib
    digest = hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{vendor}-{digest}"
```

**Secondary import convention:** `src/atelier/infra/embeddings/base.py:5-22` shows the repo’s preferred minimal `Protocol` + `@runtime_checkable` style.

---

### `src/atelier/infra/code_intel/scip/store.py` (service, CRUD)

**Analog:** `src/atelier/core/capabilities/tool_supervision/store.py`

**Atomic write pattern** (`tool_supervision/store.py:17-27`)
```python
def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".~sup_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
```

**Load/save/get/set pattern** (`tool_supervision/store.py:30-92`)  
If the SCIP store is file-backed, follow this exact “load default, tolerate decode failure, set/get helpers, save via atomic write” structure.

**Secondary interface reference:** `src/atelier/infra/storage/base.py:24-123` for protocol-style store docstrings if a formal `SymbolIntelStore` interface is introduced.

---

### `src/atelier/infra/code_intel/scip/indexer.py` (service, file-I/O)

**Analog:** `src/atelier/core/capabilities/semantic_file_memory/indexer.py`

**File-backed cache/index pattern** (`semantic_file_memory/indexer.py:26-37`, `semantic_file_memory/indexer.py:49-126`)
```python
def _atomic_write(path: Path, data: str) -> None: ...

class FileIndex:
    def __init__(self, root: Path) -> None:
        self._path = Path(root) / _CACHE_FILENAME
    ...
    def get(self, path: Path) -> dict[str, Any] | None:
        ...
        if current_hash != stored_hash:
            return None
```

**Language detection fallback** (`tree_sitter/tags.py:82-106`)
```python
def detect_language(path: Path) -> str | None:
    return {".py": "python", ".js": "javascript", ".ts": "typescript", ...}.get(path.suffix)

def extract_tags(file_path: str | Path, language: str | None = None) -> list[Tag]:
    ...
    if resolved_language == "python":
        try:
            return _python_tags(path, text)
        except SyntaxError:
            return []
```

**Planner note:** use the same “safe fallback, no crash on parse failure, pure read/index step” behavior for initial SCIP ingestion.

---

### `tests/core/test_code_context.py` (test, request-response)

**Analog:** `tests/core/test_code_context.py`

**Fixture repo pattern** (`test_code_context.py:9-32`)  
Build tiny repos inline with `tmp_path`, then assert behavior via public engine APIs.

**Regression shape to preserve** (`test_code_context.py:86-115`, `test_code_context.py:163-180`)
```python
first = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
second = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
assert first["cache_hit"] is False
assert second["cache_hit"] is True
...
assert search_payload["provenance"] == "local"
assert cached_search["provenance"] == "cached"
```

---

### `tests/gateway/test_p0_mcp_surfaces.py` (test, request-response)

**Analog:** `tests/gateway/test_p0_mcp_surfaces.py`

**Gateway smoke pattern** (`test_p0_mcp_surfaces.py:56-73`)
```python
first = tool_code({"op": "search", "repo_root": str(tmp_path), "query": "OrderService", "budget_tokens": 4000})
second = tool_code({"op": "search", "repo_root": str(tmp_path), "query": "OrderService", "budget_tokens": 4000})

assert first["cache_hit"] is False
assert second["cache_hit"] is True
assert "tokens_saved" in first
assert first["provenance"] == "local"
assert second["provenance"] == "cached"
```

**Planner note:** add new `tool_code` search params by extending this surface, not by renaming or replacing it.

---

### `tests/gateway/test_mcp_tool_handlers.py` (test, request-response)

**Analog:** `tests/gateway/test_mcp_tool_handlers.py`

**End-to-end MCP call pattern** (`test_mcp_tool_handlers.py:446-498`)
```python
indexed = _result(_call("code", {"op": "index", "repo_root": str(tmp_path)}))
searched = _result(_call("code", {"op": "search", "repo_root": str(tmp_path), "query": "alpha"}))
cached_search = _result(_call("code", {"op": "search", "repo_root": str(tmp_path), "query": "alpha"}))
...
assert cached_search["cache_hit"] is True
assert cached_search["provenance"] == "cached"
```

Use this file for routed-SCIP integration tests that must stay inside the existing `"code"` tool contract.

---

### `tests/gateway/test_savings_api.py` (test, event-driven)

**Analog:** `tests/gateway/test_savings_api.py`

**Telemetry assertion pattern** (`test_savings_api.py:944-1016`)
```python
result = {
    "cache_hit": True,
    "provenance": "cached",
    "tokens_saved": 120,
    "total_tokens": 80,
}

mcp_server._record_context_budget_for_tool("code", args, led, result)
...
assert events == [{..., "cache_hit": True, "op": "search", "provenance": "cached", ...}]
```

If Phase 1 adds snippet/ranking/provenance fields, assert they flow into savings telemetry here.

---

### `tests/benchmarks/code_intel/test_symbol_search_bench.py` (test, batch)

**Analog:** `tests/infra/test_context_savings_smoke.py`

**Smoke-test pattern** (`test_context_savings_smoke.py:15-37`)
```python
def test_context_savings_harness_smoke(tmp_path: Path) -> None:
    result = run_savings_bench(tmp_path)
    assert result.reduction_pct >= 0.0
```

**Implementation-side benchmark shape:** `src/benchmarks/swe/savings_bench.py:61-98`, `src/benchmarks/swe/savings_bench.py:250-320`  
Copy the repo’s dataclass-based benchmark result objects and deterministic `run_*_bench(...)` entrypoint style.

**Planner note:** the filename is inferred; research only guarantees a new benchmark harness under `tests/benchmarks/code_intel/`.

---

### `docs/agent-os/validation-matrix.md` (config, batch)

**Analog:** `docs/agent-os/validation-matrix.md`

**Row format** (`validation-matrix.md:3-10`)
```md
| Change surface | Minimum validation |
| --- | --- |
| Code-intel engine or MCP `code` ops | `uv run pytest ... -q && make lint && make typecheck && make test` |
```

If Phase 1 adds benchmark or SCIP-specific checks, extend this table in the same two-column style.

## Shared Patterns

### Cache invalidation and freshness
**Source:** `src/atelier/core/capabilities/code_context/engine.py:1200-1234` + `src/atelier/core/capabilities/code_context/cache.py:23-80`  
**Apply to:** routed SCIP search/index/store work
```python
return self._cache.get(
    tool_name=tool_name,
    args=args,
    index_version=self._current_index_version(),
    repo_id=self.repo_id,
)
...
index_version = self._bump_index_version(conn)
```

### Provenance and cache-hit metadata
**Source:** `src/atelier/core/capabilities/code_context/engine.py:1299-1304` + `src/atelier/gateway/adapters/mcp_server.py:2369-2438`  
**Apply to:** all new routed search responses
```python
cached["cache_hit"] = True
cached["provenance"] = "cached"
...
savings_metadata.setdefault("provenance", str(result["provenance"]))
```

### Path safety
**Source:** `src/atelier/core/capabilities/code_context/engine.py:884-897`  
**Apply to:** any repo-rooted SCIP path or snippet reader
```python
resolved.relative_to(self.repo_root)
...
raise ValueError(f"path escape denied: {value}") from exc
```

### Optional dependency / fallback handling
**Source:** `src/atelier/core/capabilities/code_context/engine.py:172-177` + `src/benchmarks/swe/savings_bench.py:37-41`  
**Apply to:** SCIP package/bootstrap logic
```python
try:
    from git import Repo
except Exception:
    return None
```

### Atomic file writes
**Source:** `src/atelier/core/capabilities/semantic_file_memory/indexer.py:26-37`  
**Apply to:** file-backed symbol stores, manifests, or benchmark outputs

### Validation gating
**Source:** `docs/agent-os/validation-matrix.md:3-16`  
**Apply to:** plan verification steps; keep targeted Phase 1 pytest command plus broad repo gates.

## No Exact Analog Found

| File | Role | Data Flow | Fallback |
|---|---|---|---|
| `src/atelier/infra/code_intel/scip/provider.py` | provider | request-response | Use protocol shape from `src/atelier/core/capabilities/cross_vendor_memory/base.py` and minimal interface style from `src/atelier/infra/embeddings/base.py` |
| `tests/benchmarks/code_intel/test_symbol_search_bench.py` | test | batch | Use benchmark smoke structure from `tests/infra/test_context_savings_smoke.py` plus implementation style from `src/benchmarks/swe/savings_bench.py` |

## Metadata

**Analog search scope:** `src/atelier/core/capabilities/code_context/`, `src/atelier/gateway/adapters/`, `src/atelier/infra/`, `src/benchmarks/`, `tests/core/`, `tests/gateway/`, `tests/infra/`, `docs/agent-os/`  
**Pattern extraction date:** 2026-05-18
