# Phase 3 Research — Semantic Recall & Relationship Navigation

**Date:** 2026-05-19
**Phase:** 03-semantic-recall-relationship-navigation
**Requirements:** `DISC-03`, `DISC-04`, `NAVG-03`

## Summary

Phase 3 should stay on existing MCP surfaces:

- `03-01` adds semantic / hybrid ranking inside `code op="search"` rather than a new search tool.
- `03-02` should land as `memory op="recall_symbol"` and compose symbol resolution, memory, traces, and lightweight related evidence.
- `03-03` should extend the routed SCIP path with call-edge support and add `code op="callers"` / `code op="callees"`.

The safest brownfield approach is the same one used in Phases 1 and 2: keep `mcp_server.py` and `engine.py` thin, push new ranking/recall/graph logic into small helpers, and reuse the existing cache, budget, routed-provider, and benchmark seams.

## Recommended decomposition

### 03-01 — Function-level embeddings and hybrid ranking inside symbol search

- Keep exact identifier-style queries on the current routed lexical path.
- Add semantic / hybrid search only for explicit `mode != "lexical"` or auto-detected natural-language queries.
- Reuse Phase 1 search cache and budget packing, but widen cache keys for the new search mode.
- Prefer a code-intel-local embedder helper over blindly reusing the global embedder factory, because the current factory defaults to `NullEmbedder`.

### 03-02 — Symbol-linked recall bundle on existing memory/code surfaces

- Use `memory op="recall_symbol"` as the public surface.
- Resolve the target symbol through `CodeContextEngine`, then assemble a fused bundle from:
  - definition
  - matching memory blocks
  - archival memory passages
  - recent traces
  - decision doc excerpts
  - related tests
- Default the low-token bundle to `["definition", "memory"]`; keep traces, decisions, and tests as opt-in heavier includes.
- Reuse M4 symbol-edit memory signals instead of inventing a new symbol-memory schema.

### 03-03 — Caller and callee traversal from the SCIP call graph

- Extend the trusted SCIP JSON artifact shape with call-edge payloads before adding new engine/MCP branches.
- Add routed provider methods and engine traversal helpers for `callers` / `callees`.
- Scope Phase 3 to routed SCIP-backed traversal with structured empty/unavailable behavior when call-edge data is absent; do not invent a new live-LSP fallback path here.
- Keep defaults cheap: `depth=1`, `snapshot=False`.

## Reusable seams

| Seam | Use in Phase 3 |
| --- | --- |
| `CodeContextEngine.tool_search()` + existing search wrapper flow | Add semantic/hybrid search modes without replacing current lexical behavior |
| `BudgetPacker` + Phase 1/2 budget discipline | Keep semantic search, recall bundles, and call graphs low-token by default |
| `RetrievalCache` + `engine_state` / `index_version` invalidation | Reuse cache/versioning for mode-specific search results and new routed graph data |
| `SymbolIntelStore` provider routing | Preserve existing routed lexical behavior and extend only where Phase 3 needs new provider methods |
| `ScipArtifactReader` / `ScipSymbolIntelProvider` | Extend existing trusted artifact loading with call-edge payloads |
| `ArchivalRecallCapability` | Reuse passage recall for the memory half of `recall_symbol` |
| `SqliteMemoryStore.list_blocks()` / `search_passages()` | Fuse block-linked symbol memory with archival passage recall |
| `ContextStore.list_traces()` | Pull trace excerpts without adding any new trace tool |
| `src/benchmarks/code_intel/*` pattern | Add semantic search, recall-symbol, and call-graph benchmarks in the same fixture-driven style |

## Concrete landing zones

### 03-01

- Production:
  - `src/atelier/core/capabilities/code_context/engine.py`
  - new helper module under `src/atelier/core/capabilities/code_context/` for embedding/ranking logic
  - `src/atelier/gateway/adapters/mcp_server.py` for additive `mode` wiring if needed
  - `src/atelier/infra/storage/vector.py` and/or existing local embedding helpers for vector persistence and similarity
- Tests:
  - `tests/core/test_code_context.py`
  - `tests/gateway/test_mcp_tool_handlers.py`
  - `tests/benchmarks/code_intel/test_symbol_search_bench.py`

### 03-02

- Production:
  - `src/atelier/gateway/adapters/mcp_server.py`
  - new helper module under `src/atelier/core/capabilities/archival_recall/` or adjacent recall-specific code
  - existing memory/code-context seams for symbol resolution and bundle assembly
  - `src/atelier/core/foundation/store.py` for trace querying
- Tests:
  - `tests/gateway/test_mcp_memory_tools.py`
  - focused new recall-bundle tests near memory/code-context coverage
  - `tests/core/test_sqlite_memory_store.py` for block/passage edge cases

### 03-03

- Production:
  - `src/atelier/gateway/adapters/mcp_server.py`
  - `src/atelier/core/capabilities/code_context/engine.py`
  - `src/atelier/core/capabilities/code_context/intel_store.py`
  - `src/atelier/infra/code_intel/scip/reader.py`
  - `src/atelier/infra/code_intel/scip/adapter.py`
- Tests:
  - `tests/infra/code_intel/scip/test_scip_adapter.py`
  - `tests/core/test_code_context.py`
  - `tests/gateway/test_mcp_tool_handlers.py`

## Brownfield constraints

- Keep `src/atelier/gateway/adapters/mcp_server.py` to literal/argument widening plus immediate delegation.
- Keep `src/atelier/core/capabilities/code_context/engine.py` as a coordinator; move ranking, recall assembly, and graph traversal details into helpers where possible.
- Do not weaken the Phase 1/2 exact-match-first search behavior by making semantic search the default for identifier-like queries.
- Reuse routed artifact invalidation patterns instead of inventing new refresh logic.
- Keep SCIP extensions repo-local and fixture-friendly; preserve current trust and size guards.

## Key defaults and decisions

### M7 home

**Recommendation:** `memory op="recall_symbol"`

Why:

1. `tool_memory` already owns `archive` and `recall`.
2. `tool_code` is already the busiest Phase 1/2 brownfield surface.
3. Existing symbol-follow-through memory from M4 is stored as memory blocks keyed by symbol id.
4. The M7 milestone itself already leans toward the memory surface.

### M6 embedding default

- Prefer the existing local embedder/vector helpers as the Phase 3 default.
- Do not require a new external package or network dependency for Phase 3 planning.
- Keep semantic ranking local to code-intel unless there is a deliberate repo-wide embedder-policy change.

### M8 fallback scope

- If call-edge data is absent, return structured empty/unavailable behavior.
- Do not widen Phase 3 to a new live-LSP fallback path.

## Risks

| Risk | Why it matters | Planning default |
| --- | --- | --- |
| Provider-first routing can bypass local semantic ranking | M6 can become a no-op on healthy routed repos if semantic logic only lives in local fallback | Make semantic/hybrid ranking explicit in the search path rather than hiding it solely behind local fallback |
| Current embedder factory defaults to `NullEmbedder` | Blind factory reuse can silently degrade semantic search | Keep M6 embedder selection local or explicitly harden the factory |
| Symbol-linked memory is split across blocks and passages | M7 can miss useful edit memories if it only queries one channel | Fuse block lookup and passage recall |
| Current SCIP artifact schema has no call edges | M8 needs real data-model work before new ops can function | Extend artifact schema first, then provider/engine/MCP |
| `mcp_server.py` and `engine.py` are already hotspots | Large inline changes will raise merge/regression risk | Use helper modules and thin dispatch branches |

## Validation strategy

### Quick automated run

```bash
uv run pytest -q \
  tests/core/test_code_context.py \
  tests/infra/code_intel/scip/test_scip_adapter.py \
  tests/gateway/test_mcp_memory_tools.py \
  tests/gateway/test_mcp_tool_handlers.py
```

### Planned additions

- `03-01`
  - natural-language fixture tests for `mode="auto"` / `mode="semantic"`
  - cache-key widening coverage
  - exact-name regression protection
  - benchmark extension for semantic query fixtures
- `03-02`
  - block + passage fusion
  - definition-preserving budget packing
  - trace filtering
  - decision word-boundary matching
  - related-test discovery
  - dedicated `recall_symbol` benchmark
- `03-03`
  - depth-1 / depth-2 traversal
  - cycle handling
  - no-call-edge fallback behavior
  - cheap token-budget benchmark for default `depth=1`

## Planning notes

- Reuse the existing `src/benchmarks/code_intel/` fixture style instead of inventing one-off performance checks.
- Keep new public behavior additive on the current `code` and `memory` tools.
- Prefer deterministic, fixture-driven validation over environment-sensitive live integrations.
- Phase 3 should be planned as three sequential plans with tight interfaces between them:
  1. semantic ranking primitive
  2. symbol-linked recall bundle
  3. call-graph traversal
