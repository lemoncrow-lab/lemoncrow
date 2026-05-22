---
phase: 2
slug: structural-discovery-symbol-safe-change-flows
status: ready
created: 2026-05-18
source: reconstructed-from-gsd-phase-researcher
---

# Phase 2 - Research

## Scope

Phase 2 delivers four linked capabilities on Atelier's existing MCP surfaces:

1. `code op="pattern"` structural search and rewrite support (M5)
2. low-token default hardening and diagnostic follow-through across the new code-intel paths (M12)
3. symbol-safe edits on the existing `edit` surface (M4)
4. `code op="usages"` reference navigation on the existing `code` surface (M3)

Mapped requirements: `DISC-01`, `DISC-02`, `NAVG-02`.

## Recommended Ordering

1. **M5 first** - add structural pattern search/rewrite on `code op="pattern"`.
2. **M12 second** - freeze cache/budget/default policy and add diagnostic follow-through, but treat this as a partial close until M4 and M3 land.
3. **M4 third** - add symbol-safe edit descriptors on the existing `edit` surface.
4. **M3 fourth** - add usages navigation after edit/span plumbing and routed symbol seams are stable.

## Why This Order

- The active code-intel plan orders the next milestones as `M5 -> M12 -> M4 -> M3`.
- M5 is the most isolated step: it can land as a thin `tool_code` branch plus a new infra adapter package without reopening Phase 1 routing choices.
- M12 is cross-cutting. It should freeze defaults, cache policy, and diagnostics early, but its full audit cannot close until usages and symbol-edit flows exist.
- M4 and M3 both touch the same brownfield hotspots in `mcp_server.py` and `engine.py`, so landing the default/budget policy first reduces churn.
- M3 is the riskiest milestone because the current routed SCIP layer is symbol-only; usages need additional artifact schema or a persisted refs index.

## Brownfield Reality After Phase 1

- Phase 1 is complete and verified at the code level, but its HUMAN-UAT file remains partial because brownfield coexistence, local SCIP realism, and default search UX were approved by operator judgment rather than fresh manual reruns.
- The worktree is clean, but `src/atelier/gateway/adapters/mcp_server.py` and `src/atelier/core/capabilities/code_context/engine.py` remain central shared landing zones.
- Phase 1 already established:
  - retrieval cache and wrapper-aware budget packing
  - routed SCIP-backed symbol lookup with safe fallback
  - snippet-free default `code op="search"` with benchmark and trace evidence

Phase 2 plans should preserve those defaults unless a milestone explicitly tightens them further.

## Landing Zones

### M5 - structural pattern search

- Thin MCP branch: `src/atelier/gateway/adapters/mcp_server.py`
- New infra package: `src/atelier/infra/code_intel/astgrep/`
- Cache/budget integration: `src/atelier/core/capabilities/code_context/cache.py`, `budget.py`, and shared wrapper helpers in `engine.py`
- Benchmark extension path: `src/benchmarks/code_intel/` and `tests/benchmarks/code_intel/`

### M12 - default and diagnostic hardening

- `src/atelier/core/capabilities/code_context/cache.py`
- `src/atelier/core/capabilities/code_context/budget.py`
- `src/atelier/core/capabilities/code_context/engine.py`
- `src/atelier/gateway/adapters/mcp_server.py` for additive diagnostics such as cache status/invalidation and low-token defaults

### M4 - symbol-safe edits

- Primary edit logic: `src/atelier/core/capabilities/tool_supervision/rich_edit.py`
- Symbol resolution/span ownership seam: `src/atelier/core/capabilities/code_context/engine.py`
- Existing diff/ledger path: `src/atelier/gateway/adapters/mcp_server.py::_compute_and_record_diffs`
- Test analogs: `tests/core/test_rich_edit.py`, `tests/gateway/test_mcp_jsonrpc_e2e.py`

### M3 - usages navigation

- MCP branch: `src/atelier/gateway/adapters/mcp_server.py`
- New engine method(s): `src/atelier/core/capabilities/code_context/engine.py`
- Routed backend extension: `src/atelier/infra/code_intel/scip/{reader.py,adapter.py}`
- Existing routed tests: `tests/infra/code_intel/scip/test_scip_adapter.py`
- Fallback analog: `src/atelier/infra/tree_sitter/tags.py` already emits `reference` tags, but only as shallow fallback data

## Key Risks

### 1. Current routed SCIP support is symbol-only

`ScipArtifactReader` currently loads symbol payloads only. Phase 2 cannot ship `code op="usages"` by just adding a new MCP branch; it must extend the routed artifact/schema story or add a second persisted references index.

### 2. Symbol IDs are unstable across edits

Current symbol IDs include file offsets and content hash material. Reformatting or edits can invalidate IDs. M4 and M3 should treat symbol IDs as session-local unless the plan introduces a stability strategy.

### 3. `rich_edit.py` has no code-intel seam yet

`rich_edit.py` is currently a file transformer, not a code-intel orchestrator. Pulling gateway logic directly into it would violate the current core/gateway split. Plans need an injected helper or new core service seam.

### 4. ast-grep binary availability is unresolved locally

`ast-grep` is not installed in the current Linux environment, and `sg` resolves to the wrong binary (`/usr/bin/sg`). Phase 2 must prefer explicit `ast-grep` binary discovery and keep failure/fallback behavior explicit.

### 5. M12 can be declared "done" too early

The M12 milestone spans defaults and diagnostics for operations that do not exist until M4 and M3 land. The planner must word M12 as **core freeze first, final follow-through later**.

## Constraints To Preserve

- Stay on existing MCP surfaces. Extend `code` and `edit`; do not add new top-level tools.
- Keep `mcp_server.py` thin. New heavy logic belongs in `core/` or `infra/`.
- Preserve low-token defaults, benchmark evidence, and trace recording as completion requirements.
- Treat Phase 1 budget/search behavior as the baseline. Phase 2 should build on it rather than reopening the same cost questions.

## Validation Architecture

- Extend the existing benchmark landing zone under `src/benchmarks/code_intel/` and `tests/benchmarks/code_intel/`; do not create a parallel benchmark stack.
- Keep the current test tier split:
  - core behavior in `tests/core/`
  - MCP boundary behavior in `tests/gateway/`
  - routed backend behavior in `tests/infra/`
- Required benchmark slices:
  - structural pattern flow versus text-search/read/edit baseline
  - usages flow versus grep/read baseline
  - payload-size and default-policy checks for M12
- Reuse Phase 1 targeted suites where possible:
  - `tests/core/test_code_context.py`
  - `tests/gateway/test_p0_mcp_surfaces.py`
  - `tests/gateway/test_mcp_tool_handlers.py`
  - `tests/infra/code_intel/scip/test_scip_adapter.py`
  - `tests/benchmarks/code_intel/test_symbol_search_bench.py`

## Resolved Planning Decisions

1. **Usages artifact strategy:** Phase 2 will extend the current persisted JSON `.scip` fixture/artifact shape with reference payloads instead of introducing a second persisted references index. This keeps M3 on the existing routed seam and avoids inventing a parallel store before the scale phases.
2. **Rich-edit seam strategy:** Phase 2 will introduce a dedicated core helper (`symbol_edit.py`) for symbol resolution and stale-target checks. `rich_edit.py` remains a dispatcher and atomic file writer.
3. **M12 split:** the early M12 plan owns cache/budget/default-policy freeze plus additive diagnostics for shipped flows. The M4 and M3 plans own the follow-through checks for symbol-edit and usages defaults, diagnostics, trace capture, and final validation closeout.
4. **ast-grep availability strategy:** Phase 2 will use explicit env override first, exact `ast-grep` binary discovery second, and a pinned managed bootstrap/download path third (version/checksum manifest owned in the new ast-grep package). Only after those paths fail should the runtime return `tool_unavailable`.

## Planning Guidance

- Plan M5 as the narrowest new infra landing zone.
- Plan M12 as a hardening pass that establishes policy and diagnostics without overclaiming full milestone closure.
- Give M4 explicit brownfield guardrails around symbol identity and edit span ownership.
- Give M3 explicit schema/backend work; do not phrase it as a trivial adapter branch.
- The planning ambiguity gates for usages storage, rich-edit seams, M12 split, and ast-grep bootstrap are now resolved above and should be treated as locked planning inputs.
