---
phase: 3
slug: semantic-recall-relationship-navigation
status: ready
created: 2026-05-19
source: phase-planning
---

# Phase 3 - Validation Strategy

> Per-phase validation contract for semantic search, symbol-linked recall, and call-graph navigation on existing MCP surfaces.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + existing repo make targets |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `uv run pytest tests/core/test_code_context.py tests/core/capabilities/archival_recall/test_symbol_recall.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_mcp_memory_tools.py tests/infra/code_intel/scip/test_scip_adapter.py tests/benchmarks/code_intel/test_symbol_search_bench.py tests/benchmarks/code_intel/test_recall_symbol_bench.py tests/benchmarks/code_intel/test_call_graph_bench.py -q` |
| **Full suite command** | `make lint && make typecheck && make test` |
| **Estimated runtime** | ~30-300 seconds |

---

## Wave Order

| Wave | Plans | Why |
|------|-------|-----|
| **Wave 1** | `03-01` | Introduces the mode-aware semantic ranking primitive that later recall/traversal flows reuse indirectly through existing symbol search expectations. |
| **Wave 2** | `03-02` | Extends the existing `memory` surface after M6 search behavior and benchmarks are in place. |
| **Wave 3** | `03-03` | Extends routed SCIP contracts and overlapping `mcp_server.py` / `engine.py` hotspots for caller/callee traversal. |

---

## Sampling Rate

- **After every task commit:** run the smallest targeted pytest subset for the touched plan surface.
- **After every wave:** run the Phase 3 quick command plus the benchmark test added by that wave.
- **Before verification:** run `make lint && make typecheck && make test`, tracking unrelated pre-existing failures separately if they remain outside the phase scope.
- **Max feedback latency:** 300 seconds.

---

## Per-Plan Verification Map

| Plan | Milestone | Requirement | Secure / correct behavior | Expected automated coverage |
|------|-----------|-------------|---------------------------|-----------------------------|
| `03-01` | M6 | `DISC-03` | `code op="search"` stays on the existing MCP surface, adds semantic/hybrid ranking beside lexical exact-name behavior, keeps cache/budget discipline intact, and records trace evidence against `M6-semantic-rank.md` | `tests/core/test_code_context.py`, `tests/gateway/test_mcp_tool_handlers.py`, `tests/gateway/test_p0_mcp_surfaces.py`, `tests/benchmarks/code_intel/test_symbol_search_bench.py` |
| `03-02` | M7 | `DISC-04` | `memory op="recall_symbol"` returns a fused symbol-linked bundle with low-token defaults, reuses existing memory/trace surfaces, and does not add a parallel `code op="recall"` path | `tests/core/capabilities/archival_recall/test_symbol_recall.py`, `tests/gateway/test_mcp_memory_tools.py`, `tests/benchmarks/code_intel/test_recall_symbol_bench.py` |
| `03-03` | M8 | `NAVG-03` | `code op="callers"` / `op="callees"` traverse routed SCIP call edges with depth-limited, cycle-safe behavior and explicit no-call-edge responses instead of a live-LSP fallback | `tests/core/test_code_context.py`, `tests/gateway/test_mcp_tool_handlers.py`, `tests/gateway/test_p0_mcp_surfaces.py`, `tests/infra/code_intel/scip/test_scip_adapter.py`, `tests/benchmarks/code_intel/test_call_graph_bench.py` |

---

## Wave 0 Requirements

- [ ] Reuse the existing code-intel benchmark landing zone under `src/benchmarks/code_intel/` and `tests/benchmarks/code_intel/`.
- [ ] Keep Phase 1 and Phase 2 suites green while extending the same surfaces:
  - `tests/core/test_code_context.py`
  - `tests/gateway/test_p0_mcp_surfaces.py`
  - `tests/gateway/test_mcp_tool_handlers.py`
  - `tests/gateway/test_mcp_memory_tools.py`
  - `tests/infra/code_intel/scip/test_scip_adapter.py`
- [ ] Add or extend focused Phase 3 suites for:
  - semantic/hybrid ranking and exact-name regression protection
  - symbol-linked recall bundle assembly and MCP memory dispatch
  - caller/callee traversal, cycle handling, and no-call-edge behavior
- [ ] Keep `src/atelier/gateway/adapters/mcp_server.py` and `src/atelier/core/capabilities/code_context/engine.py` thin and additive; any growth beyond dispatch/coordinator work is a validation failure.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Review brownfield coexistence in `mcp_server.py` and `engine.py` | `DISC-03`, `DISC-04`, `NAVG-03` | Automated tests prove behavior, not whether shared hotspots stayed narrow and additive | Review final diffs and confirm heavy ranking/recall/graph logic lives in helper modules, not inline in the hotspots. |
| Exercise an intent-first agent workflow | `DISC-03`, `DISC-04`, `NAVG-03` | Benchmarks and unit tests do not prove real operator UX | 1. Run a natural-language `code op="search"` query where the exact name is unknown. 2. Run `memory op="recall_symbol"` on the returned target. 3. Run `code op="callers"` or `op="callees"` on that symbol. 4. Confirm the workflow stays on existing MCP surfaces without grep-first or line-number fallback. |
| Confirm missing call-edge data stays explicit | `NAVG-03` | Automated fixtures prove shaped responses, but not operator understanding of the degraded mode | Run callers/callees on a repo or fixture without SCIP call edges and confirm the response is structured as empty/unavailable with no invented live-LSP fallback language. |

---

## Wave Trace Evidence

- `03-01` closes with a recorded trace referencing `docs/plans/active/code-intel/M6-semantic-rank.md` after the semantic benchmark and exact-name regression gate pass.
- Keep the trace payload tied to the shipped `code op="search"` surface rather than a helper-only implementation detail so validation evidence matches the public behavior.
- `03-02` closes with a recorded trace referencing `docs/plans/active/code-intel/M7-recall-symbol.md` after the recall benchmark proves the default definition-plus-memory bundle stays under budget and smaller than the expanded/manual paths.
- `03-03` closes with a recorded trace referencing `docs/plans/active/code-intel/M8-call-graph.md` after the call-graph benchmark proves the shipped `depth=1, snapshot=False` default stays under budget and smaller than the deeper snapshot path.

---

## Source Coverage Audit

| Source Type | Item | Covered By | Status |
|-------------|------|------------|--------|
| GOAL | Agents recover intent, prior context, and symbol relationships before changing code | `03-01`, `03-02`, `03-03` | covered |
| REQ | `DISC-03` semantic fallback when exact names are insufficient | `03-01` | covered |
| REQ | `DISC-04` symbol-linked memory recall on existing surfaces | `03-02` | covered |
| REQ | `NAVG-03` callers/callees on `code` ops | `03-03` | covered |
| RESEARCH | M6 stays on `code op="search"` with semantic/hybrid layered beside lexical exact-name behavior | `03-01` | covered |
| RESEARCH | M7 lives on `memory op="recall_symbol"` with default include `["definition","memory"]` | `03-02` | covered |
| RESEARCH | M8 extends routed SCIP artifacts/provider contract with call edges; no new top-level tool and no live-LSP fallback | `03-03` | covered |
| RESEARCH | `mcp_server.py` and `engine.py` remain thin and additive brownfield hotspots | all plans + manual check | covered |
| RESEARCH | Reuse cache, budget, benchmark, and validation seams already in the repo | all plans | covered |
| CONTEXT | No Phase 3 `CONTEXT.md` file was provided; use ROADMAP + RESEARCH defaults | all plans | covered |

---

## Validation Sign-Off

- [ ] Phase 3 reuses existing repo validation tooling
- [ ] Feedback latency target remains under 300 seconds
- [ ] Manual-only checks are explicitly recorded
- [ ] Wave 0 benchmark/test gaps closed
- [ ] Phase 3 benchmark and trace evidence captured
- [ ] Final approval recorded

**Approval:** pending execution, verification, and human sign-off
