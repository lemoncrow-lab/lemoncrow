# Phase 5 Research — Scale Decision & Extended Retrieval Reach

**Date:** 2026-05-19
**Phase:** 05-scale-decision-and-extended-retrieval-reach
**Requirements:** `ENBL-03`, `SCAL-01`, `SCAL-02`

## Summary

Phase 5 should stay aligned with the roadmap as three plans:

- `05-01` — M18 build-vs-integrate checkpoint
- `05-02` — M16 validated scale backend routing
- `05-03` — M17 partial cross-language edge resolution

The main repo-specific finding is that M16 cannot be treated as a simple `SymbolIntelStore` backend swap. Atelier currently has **two separate retrieval stacks**:

- `tool_code` / `CodeContextEngine` for symbol-shaped code-intel results
- `tool_search` / `smart_search` for text and file-match search

That means the Phase 5 checkpoint must decide whether scale routing applies to `search`, `code`, or both before any backend integration starts.

## Recommended decomposition

### 05-01 — M18 build-vs-integrate checkpoint

- Keep this plan **doc-first** with a small executable evaluation harness only.
- Record the repo-specific decision answers before any M16 implementation:
  - does the chosen scale backend serve `search`, `code`, or both?
  - who owns backend lifecycle when `CodeContextEngine` is rebuilt per call?
  - is the backend returning symbol-shaped results, text/file matches, or both?
- Default recommendation: treat scale routing as a **text-search backend first**, while identifier/name-first symbol search stays on existing local/SCIP/semantic paths unless a later adapter proves symbol-shape parity.

### 05-02 — M16 validated scale backend routing

- Scope this as **validated large-repo text search routing first**.
- Land the backend in a new `src/atelier/infra/code_intel/zoekt/` package, but integrate it through the existing `search` stack before widening `code op="search"`.
- If `code op="search"` exposes any scale metadata, keep it additive-only and only for lexical/text-shaped flows.
- Split the work internally into:
  - backend ownership/bootstrap
  - routing integration on the text-search path
  - additive backend metadata, benchmarks, and validation

### 05-03 — M17 partial cross-language edges

- Keep this separate from M16 implementation.
- Restrict Phase 5 to **literal-only static edges**:
  - Python `ctypes` / `cffi`
  - literal `importlib.import_module("...")`
  - literal subprocess-to-`.py` entrypoints
- Do not broaden into general interprocedural flow, TS/Go graph resolution, or runtime tracing.
- Split the work internally into:
  - cross-language edge storage + resolver infra
  - engine hydration on `symbol` and `usages`
  - fixture tests and benchmark smoke

## Reusable seams

| Seam | Use in Phase 5 |
| --- | --- |
| `_sync_external_artifact_state()` in `engine.py` | Reuse the artifact-signature freshness pattern for backend/cross-language artifacts |
| Phase 4 thin-adapter split | Keep `engine.py` on schema/cache/dispatch only; push heavy logic into dedicated infra adapters |
| ast-grep managed binary bootstrap | Best current repo pattern if a managed backend binary path is chosen |
| import extraction / `imports` table | Supporting context for M17, but not a substitute for explicit cross-language edge rows |
| `src/benchmarks/code_intel/` + `tests/benchmarks/code_intel/` | Reuse the existing benchmark/assertion pattern for M16 and M17 |

## Concrete landing zones

### 05-01

- `docs/plans/active/code-intel/M18-bvi-checkpoint.md`
- optional evaluation harness under `src/benchmarks/code_intel/`
- matching assertions under `tests/benchmarks/code_intel/`

### 05-02

- `src/atelier/core/capabilities/tool_supervision/smart_search.py`
- `src/atelier/core/capabilities/tool_supervision/search_read.py`
- new `src/atelier/infra/code_intel/zoekt/`
- additive-only metadata in `src/atelier/gateway/adapters/mcp_server.py` if surfaced publicly

### 05-03

- new `src/atelier/infra/code_intel/cross_lang/`
- `src/atelier/core/capabilities/code_context/engine.py` for thin schema/hydration hooks only
- `src/atelier/core/capabilities/code_context/models.py` for typed additive fields like `confidence` / `edge_kind`
- `_SYMBOL_OPTIONAL_KEYS` and `_USAGES_OPTIONAL_KEYS` in `engine.py` so budget packing preserves new fields

## Brownfield constraints

- `engine.py` is already a hotspot; do not add backend/resolver logic inline.
- `mcp_server.py` must stay limited to additive params, delegation, and possible lifecycle plumbing only.
- `tool_search()` auto-indexes local symbol state before normal routing today, so naive M16 insertion inside `search_symbols()` would still pay local indexing cost first.
- Do not let Phase 5 absorb Phase 6 `scope="external"` or multi-repo concerns.
- Broad repo tests are not a reliable green gate; keep validation targeted.
- Stick to `uv run`; the local environment does not reliably match bare `python3`.

## Key defaults and decisions

### Scale backend

- Default to **text-search backend first**, not symbol-provider replacement.
- Keep name-first `code op="search"` on local/SCIP/semantic flows unless symbol-shape parity is proven.
- Add a separate backend lifecycle owner outside ephemeral `CodeContextEngine` instances if the backend is long-lived.
- Prefer existing stdlib/`urllib3` over introducing `httpx`.

### Cross-language

- Default to literal-string static cases only.
- Keep M17 additive on existing `code op="symbol"` and `code op="usages"` surfaces only.
- Surface low-confidence items, but tag them clearly.

## Risks

| Risk | Why it matters | Planning default |
| --- | --- | --- |
| M16 milestone text assumes `SymbolIntelStore` backend integration | Live repo contracts show `SymbolIntelStore` is symbol-only while `search` is on a separate stack | Decide in `05-01` that scale routing serves text search first unless symbol-shape parity is proven |
| Backend lifetime mismatch | `CodeContextEngine` is created per call today | Solve lifecycle outside engine instances before full M16 work |
| Zoekt bootstrap ambiguity | Official docs verified Go/container/API flows, not an official prebuilt managed-binary path | Treat managed binary as a repo choice, not a documented default |
| Environment lacks Go | Building Zoekt from source locally is not a safe default | Prefer evaluation via container or documented remote API path during checkpoint work |
| `UsageReference` cannot carry extra M17 fields cleanly | Cross-language `usages` need confidence/edge typing | Explicitly update typed models in `05-03` |
| Phase 6 scope creep | “Cross-language” can accidentally absorb dependency/workspace routing | Keep Phase 5 limited to static cross-language edges only |

## Validation strategy

### 05-02

- `uv run pytest tests/gateway/test_p0_mcp_surfaces.py tests/core/test_code_context.py -q`
- targeted infra suite under `tests/infra/code_intel/zoekt/`
- targeted benchmark assertions under `tests/benchmarks/code_intel/test_zoekt_*.py`

### 05-03

- `uv run pytest tests/core/test_code_context.py tests/gateway/test_p0_mcp_surfaces.py -q`
- targeted infra suite under `tests/infra/code_intel/cross_lang/`
- targeted benchmark/assertion coverage in `tests/benchmarks/code_intel/`

### Shared

- Benchmark the **warm path**, backend provenance/routing, and token-budget impact, not just raw latency.
- Prefer fixture repos over live Atelier examples for M17.
- Treat `make test` as informational only if unrelated repo failures remain outside Phase 5 scope.

## Planning notes

- `05-01` must explicitly record whether the chosen backend serves `search` only or both `search` and `code`.
- If backend provenance is exposed, make it additive metadata only.
- Safest M17 response model:
  - `symbol` gets `cross_lang_refs`
  - `usages` appends cross-language references into the existing grouped shape
  - low-confidence items remain visible but tagged
- Do not build a second giant router in `mcp_server.py`; reuse the accepted thin-adapter pattern from Phase 4.
