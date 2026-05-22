---
phase: 2
slug: structural-discovery-symbol-safe-change-flows
status: ready
created: 2026-05-18
source: reconstructed-from-gsd-pattern-mapper
---

# Phase 2 - Pattern Map

## Highest-Value Analogs

| Planned surface | Best analog | Why |
|-----------------|-------------|-----|
| `src/atelier/gateway/adapters/mcp_server.py` add `code op="pattern"` / `code op="usages"` | `src/atelier/gateway/adapters/mcp_server.py` existing `tool_code()` branch | Keep the MCP layer thin: validate args, delegate to engine immediately, preserve the existing `code` tool surface. |
| `src/atelier/core/capabilities/code_context/engine.py` add pattern/usages wrappers | Existing cached and budgeted tool wrappers in `engine.py` | Phase 1 already established the cache/budget/provenance/finalization shape that new code-intel ops should reuse. |
| new `src/atelier/infra/code_intel/astgrep/*` package | `src/atelier/infra/code_intel/scip/*` | Closest existing infra package with binary discovery, trusted artifact/result loading, and refresh/invalidation seams. |
| structural pattern benchmarks | `src/benchmarks/code_intel/symbol_search_bench.py` and `tests/benchmarks/code_intel/test_symbol_search_bench.py` | Reuse the Phase 1 code-intel benchmark landing zone instead of creating a parallel stack. |
| symbol-safe edits in `rich_edit.py` | existing atomic edit + rollback flow in `src/atelier/core/capabilities/tool_supervision/rich_edit.py` | Resolve symbol/span ownership first, then reuse the existing write/rollback machinery. |
| edit gateway + e2e coverage | `tests/core/test_rich_edit.py` and `tests/gateway/test_mcp_jsonrpc_e2e.py` | Closest existing edit-path unit and end-to-end analogs. |
| routed usages backend | `intel_store.py` + `scip/adapter.py` + `tree_sitter/tags.py` | Closest routed-provider/fallback pattern, with `tree_sitter` references as shallow local fallback. |
| low-token hardening / diagnostics | `cache.py`, `budget.py`, `engine.py`, `mcp_server.py` telemetry propagation, and `tests/gateway/test_mcp_tool_handlers.py` | Existing cache-hit / provenance / tokens-saved / total-token pattern should remain the default across new ops. |

## File Classification Map

| Expected file/change | Role | Closest analog | Match |
|----------------------|------|----------------|-------|
| `src/atelier/gateway/adapters/mcp_server.py` | gateway adapter | same file `tool_code()` | exact |
| `src/atelier/core/capabilities/code_context/engine.py` | core service | same file tool wrappers | exact |
| `src/atelier/core/capabilities/code_context/cache.py` | cache utility | same file | exact |
| `src/atelier/core/capabilities/code_context/budget.py` | budget utility | same file | exact |
| `src/atelier/core/capabilities/tool_supervision/rich_edit.py` | edit service | same file | exact |
| `src/atelier/infra/code_intel/astgrep/binaries.py` | binary discovery | `src/atelier/infra/code_intel/scip/binaries.py` | exact-shape |
| `src/atelier/infra/code_intel/astgrep/adapter.py` | infra adapter | `src/atelier/infra/code_intel/scip/adapter.py` | exact-shape |
| `src/atelier/infra/code_intel/astgrep/indexer.py` | infra utility | `src/atelier/infra/code_intel/scip/indexer.py` | role-match |
| `src/atelier/infra/code_intel/astgrep/reader.py` or loader | infra utility | `src/atelier/infra/code_intel/scip/reader.py` | role-match |
| `src/benchmarks/code_intel/*pattern*.py` | benchmark | `src/benchmarks/code_intel/symbol_search_bench.py` | exact-shape |
| `tests/benchmarks/code_intel/test_*pattern*.py` | benchmark test | `tests/benchmarks/code_intel/test_symbol_search_bench.py` | exact-shape |
| `tests/core/test_rich_edit.py` | unit test | same file | exact |
| `tests/gateway/test_mcp_jsonrpc_e2e.py` | e2e test | same file | exact |
| `tests/core/test_code_context.py` | core test | same file | exact |
| `tests/gateway/test_p0_mcp_surfaces.py` | MCP boundary test | same file | exact |
| `tests/gateway/test_mcp_tool_handlers.py` | tool handler test | same file | exact |
| `tests/infra/code_intel/scip/test_scip_adapter.py` | routed backend test | same file | exact |

## Reusable Patterns

### 1. Thin `tool_code()` branching

Use the existing `tool_code()` pattern in `src/atelier/gateway/adapters/mcp_server.py`:

- validate required args in the gateway
- keep `op` dispatch additive on the existing `code` tool
- delegate heavy logic to `CodeContextEngine`

This is the right analog for both `pattern` and `usages`.

### 2. Cache + budget wrappers in `engine.py`

Follow the Phase 1 wrapper pattern:

- compute cache args from op inputs
- `_cache_get()` before work
- finalize payload through shared pack/finalization helpers
- `_cache_set()` after work

New Phase 2 flows should return the same metadata shape: `cache_hit`, `provenance`, `tokens_saved`, `total_tokens`.

### 3. Explicit binary discovery

Use `src/atelier/infra/code_intel/scip/binaries.py` as the discovery template:

- prefer env var + exact binary name
- resolve via `shutil.which()`
- fail explicitly when unavailable

For ast-grep, do **not** rely on `sg` on Linux.

### 4. Trusted infra package layout

Model the new `astgrep` package after the existing `scip` package:

- binary discovery
- narrow adapter entrypoints
- trusted result loading
- optional refresh/signature seam

### 5. Atomic edit and rollback

Symbol-safe edits should reuse the existing flow in `rich_edit.py`:

- resolve symbol/span first
- transform into concrete file edits
- reuse atomic writes and rollback behavior

Keep diff recording in the gateway through the existing `_compute_and_record_diffs` path.

### 6. Routed provider fallback

Use `src/atelier/core/capabilities/code_context/intel_store.py` as the model:

- try healthy routed providers first
- tolerate provider exceptions without breaking the request
- fall back to the local path when routed data is unavailable

This is the best analog for `code op="usages"`.

### 7. Shallow local reference fallback

`src/atelier/infra/tree_sitter/tags.py` already emits `reference` tags. It is not a full usages system, but it is the nearest local fallback analog if routed usages data is incomplete.

### 8. Benchmark shape

Follow the Phase 1 benchmark pattern:

- deterministic fixture repo
- two-path comparison
- typed result object with serialization
- threshold assertions in `tests/benchmarks/code_intel/`

## No-Close-Analog Notes

- There is no existing AST-aware rewrite adapter outside the SCIP package shape.
- There is no existing first-class grouped usages payload model yet.
- Phase 2 therefore needs new models/seams, but it should still mirror the surrounding gateway/core/infra/test patterns rather than inventing a parallel structure.
