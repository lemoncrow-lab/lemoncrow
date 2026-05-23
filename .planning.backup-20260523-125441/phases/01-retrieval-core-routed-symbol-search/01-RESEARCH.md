# Phase 1 Research: Retrieval Core & Routed Symbol Search

**Researched:** 2026-05-18
**Phase:** 1 - Retrieval Core & Routed Symbol Search
**Requirements:** FNDN-01, FNDN-02, NAVG-01

## Objective

Determine what the planner needs to know to deliver Phase 1 safely in the
existing Atelier codebase: shared retrieval cache and budget packing, routed
SCIP-backed symbol search, and hardened `code op="search"` defaults without
adding new top-level MCP tools.

## Source Inputs

- `.planning/PROJECT.md`
- `.planning/REQUIREMENTS.md`
- `.planning/ROADMAP.md`
- `.planning/STATE.md`
- `.planning/codebase/ARCHITECTURE.md`
- `.planning/codebase/STACK.md`
- `docs/plans/active/code-intel/index.md`
- `docs/plans/active/code-intel/grounding.md`
- `docs/plans/active/code-intel/M0-store.md`
- `docs/plans/active/code-intel/M1-scip-adapter.md`
- `docs/plans/active/code-intel/M2-symbol-tool.md`
- Current worktree state in `src/atelier/core/capabilities/code_context/`,
  `src/atelier/gateway/adapters/mcp_server.py`, and related tests

## Key Findings

### 1. Phase sequencing is fixed: M0 -> M1 -> M2

- `docs/plans/active/code-intel/index.md` makes M0 the prerequisite for M1-M9.
- `docs/plans/active/code-intel/grounding.md` explicitly assigns
  `SymbolIntelStore` / `SymbolIntelProvider` ownership to M1, not M0.
- `docs/plans/active/code-intel/M2-symbol-tool.md` hardens the existing
  `code op="search"` path and depends on the M0/M1 foundations.

**Planning implication:** treat Phase 1 as three ordered plans:
1. finish/harden M0 primitives
2. add routed SCIP backend in M1
3. harden `code op="search"` in M2

### 2. This is a brownfield phase already in flight

- The current worktree already contains user changes in
  `src/atelier/core/capabilities/code_context/`,
  `src/atelier/gateway/adapters/mcp_server.py`, and the related Phase 1 test
  files.
- Planning must avoid overwrite-style tasks and instead isolate work by narrow
  landing zones so the user's in-flight edits are preserved.

**Planning implication:** plans should explicitly call out brownfield-safe
editing, incremental verification, and reuse of the modified files already in
progress.

### 3. Much of M0 is partially landed already

Evidence exists for:
- `src/atelier/core/capabilities/code_context/budget.py`
- `src/atelier/core/capabilities/code_context/cache.py`
- `src/atelier/core/capabilities/code_context/engine.py`
- `src/atelier/gateway/adapters/mcp_server.py`
- `tests/core/test_code_context.py`
- `tests/gateway/test_p0_mcp_surfaces.py`
- `tests/gateway/test_mcp_tool_handlers.py`
- `tests/gateway/test_savings_api.py`

The research found working implementations or partial implementations for:
- `BudgetPacker`
- `RetrievalCache`
- engine-level wrappers around existing `code` operations
- savings/provenance metadata propagation into MCP responses

**Planning implication:** Plan 01-01 should focus on completion,
hardening, coverage, and cleanup of M0 behavior rather than starting from a
blank slate.

### 4. Phase 1 still has clear incomplete or fragile boundaries

- Budget packing can still overshoot `budget_tokens` because item packing
  happens before wrapper metadata is added in
  `src/atelier/core/capabilities/code_context/engine.py`.
- Cache freshness still depends on explicit `index_repo()` version bumps; the
  watcher/invalidation story expected by M1 is not complete yet.
- Local symbol IDs in `src/atelier/core/capabilities/code_context/engine.py`
  are still position/content-hash based rather than the stable ID shape called
  for in `docs/plans/active/code-intel/index.md`.
- `src/atelier/gateway/adapters/mcp_server.py` does not yet expose the full M2
  hardening surface (`snippet`, `snippet_lines`, `file_glob`, `scope`).

**Planning implication:** Phase 1 plans must reserve explicit verification and
follow-through for these gaps instead of assuming the current worktree is ready
to ship.

### 5. Validation and telemetry surfaces are load-bearing

The phase must preserve:
- existing MCP surface expectations in
  `tests/gateway/test_mcp_tool_handlers.py`
- code-tool savings metadata behavior in
  `tests/gateway/test_savings_api.py`
- repository validation guidance in `docs/agent-os/validation-matrix.md`

**Planning implication:** every plan should keep the current tool shape stable,
avoid new top-level MCP registrations, and verify metadata fields on the
existing `code` responses.

### 6. Benchmark infrastructure is a real planning gap

- `docs/plans/active/code-intel/index.md` expects benchmark evidence under
  `tests/benchmarks/code_intel/`.
- That directory does not exist yet; the current repo benchmark code lives in
  `src/benchmarks/`.

**Planning implication:** the phase plan needs an explicit benchmark-harness
task or prerequisite so Phase 1 can satisfy its validation gates without
discovering the missing path at the end.

### 7. Environment and dependency notes

- The local environment has `uv`, `python3`, `pytest`, `rg`, `node`, `npm`,
  and `cargo`.
- `go` is not available locally, so Phase 1 should not assume `scip-go` is part
  of the initial path on this machine.
- The research validated likely additions for this phase:
  - Sourcegraph SCIP packages for Python and TypeScript
  - `watchdog` for filesystem event handling

**Planning implication:** Phase 1 should target Python and TypeScript SCIP
workflows first and treat broader language watcher/indexer coverage as later
work.

## Code Landing Zones

### Core implementation

- `src/atelier/core/capabilities/code_context/engine.py`
- `src/atelier/core/capabilities/code_context/models.py`
- `src/atelier/core/capabilities/code_context/cache.py`
- `src/atelier/core/capabilities/code_context/budget.py`

### MCP surface

- `src/atelier/gateway/adapters/mcp_server.py`

### New Phase 1 infrastructure expected by the active plan

- `src/atelier/infra/code_intel/scip/` (M1)

### Primary regression surfaces

- `tests/core/test_code_context.py`
- `tests/gateway/test_p0_mcp_surfaces.py`
- `tests/gateway/test_mcp_tool_handlers.py`
- `tests/gateway/test_savings_api.py`
- `docs/agent-os/validation-matrix.md`

## Recommended Planning Boundaries

### Plan 01-01 - M0 hardening

Focus on:
- finishing the cache/budget integration already started in
  `code_context/`
- closing metadata and budget edge cases
- locking targeted tests for cache hit, invalidation, provenance, and savings
- deciding how the missing benchmark harness is introduced for this phase

### Plan 01-02 - M1 routed SCIP backend

Focus on:
- adding the SCIP adapter under `src/atelier/infra/code_intel/scip/`
- introducing `SymbolIntelStore` / `SymbolIntelProvider` at the M1 boundary
- keeping fallback behavior through the existing local engine path
- constraining initial indexer support to what the current environment can
  actually run

### Plan 01-03 - M2 hardened symbol search

Focus on:
- extending the current `tool_code` search surface without adding a new top-level
  tool
- shipping snippet/ranking/provenance improvements on the existing search path
- making outline-first search behavior the default agent experience

## Validation Architecture

Phase 1 validation should prove both correctness and token-savings intent.

### Required command surface

- Targeted Phase 1 regression suite:
  `uv run pytest tests/core/test_code_context.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_savings_api.py -q`
- Brown broader repo gate for Python runtime changes:
  `make lint && make typecheck && make test`

### Evidence to require in plans

- Cache/provenance metadata is asserted on existing `code` MCP responses.
- Brownfield edits preserve current `tool_code` entry points in
  `src/atelier/gateway/adapters/mcp_server.py`.
- Benchmark coverage is planned explicitly even though the expected
  `tests/benchmarks/code_intel/` path does not yet exist.
- Any SCIP package/bootstrap work is verified with environment-aware fallbacks
  and does not assume unavailable toolchains.

## Risks to Carry Into Planning

- Existing user edits make broad refactors risky.
- Stable symbol IDs are still unresolved at the Phase 1 boundary.
- Cache invalidation is incomplete until watcher/reindex behavior is defined.
- M2 API hardening can accidentally expand the public MCP surface in ways the
  grounding doc forbids.
- Benchmark expectations in the active docs are ahead of the current repo
  layout.

## Planning Summary

Phase 1 is not greenfield implementation. It is brownfield completion and
hardening of an in-flight M0 foundation, followed by a carefully isolated M1
SCIP routing layer and an M2 hardening pass on the existing `code op="search"`
surface. The plan must protect current MCP contracts, preserve the user's
in-flight edits, and explicitly close the validation/benchmark gaps that the
active code-intel docs already expect.
