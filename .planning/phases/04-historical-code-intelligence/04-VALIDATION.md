---
phase: 4
slug: historical-code-intelligence
status: human_needed
created: 2026-05-19
source: phase-planning
---

# Phase 4 - Validation Strategy

> Per-phase validation contract for deleted-history search, rename awareness, blame, churn, and temporal filtering on the existing `code` MCP surface.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + existing repo benchmark/test layout |
| **Config file** | `pyproject.toml` |
| **Quick run command** | See wave-specific commands below |
| **Full suite command** | `make lint && make typecheck && make test` |
| **Estimated runtime** | ~30-300 seconds |

---

## Wave Order

| Wave | Plans | Why |
|------|-------|-----|
| **Wave 1** | `04-01` | Resolve the `pygit2` legitimacy/bootstrap blocker and prove the isolated graveyard substrate with real git-history infra tests before any hotspot wiring. |
| **Wave 2** | `04-02` | Wire deleted-history search on the existing `code` surface, then close M14 with benchmark and trace evidence. |
| **Wave 3** | `04-03` | Build blame/churn substrate plus explicit freshness metadata propagation without touching the MCP surface or closeout evidence. |
| **Wave 4** | `04-04` | Wire `code op="blame"` and live temporal repo filtering, then close M15 with benchmark, cost-discipline, and trace evidence. |

---

## Sampling Rate

- **After every task commit:** run the smallest targeted pytest subset for the task's owned files only.
- **After every wave:** run that wave's quick command only; do not pull later-wave suites forward.
- **Before final phase verification:** run `make lint && make typecheck && make test`, tracking unrelated pre-existing failures separately.
- **Max feedback latency:** 300 seconds.

---

## Per-Plan Verification Map

| Plan | Milestone | Requirement | Secure / correct behavior | Expected automated coverage |
|------|-----------|-------------|---------------------------|-----------------------------|
| `04-01` | bootstrap for M14 | `HIST-01` | `pygit2` is pinned/importable with no hidden fallback, and the isolated graveyard substrate passes real delete/rename fixture tests before any public wiring | `tests/infra/code_intel/git_history/test_graveyard.py` plus explicit `import pygit2` check |
| `04-02` | M14 closeout | `HIST-01` | `code op="search"` serves `scope="deleted"` with additive `since` / `touched_by`, keeps the existing `items` envelope, and closes with graveyard benchmark + trace evidence | `tests/core/test_code_context.py`, `tests/gateway/test_mcp_tool_handlers.py`, `tests/gateway/test_p0_mcp_surfaces.py`, `tests/benchmarks/code_intel/test_graveyard_bench.py` |
| `04-03` | substrate for M15 | `HIST-02` | blame/churn logic and explicit index freshness metadata are proven in infra seams only; no MCP surface or benchmark promise belongs here | `tests/infra/code_intel/git_history/test_blame.py`, `tests/infra/code_intel/scip/test_scip_adapter.py` |
| `04-04` | M15 closeout | `HIST-01`, `HIST-02` | `code op="blame"` returns ownership/churn metadata, stale indexes fail explicitly, live repo search honors temporal filters, and M15 closes with benchmark + cost-discipline + trace evidence | `tests/core/test_code_context.py`, `tests/gateway/test_mcp_tool_handlers.py`, `tests/gateway/test_p0_mcp_surfaces.py`, `tests/benchmarks/code_intel/test_blame_bench.py`, `tests/benchmarks/code_intel/test_cost_discipline.py` |

---

## Wave-Specific Quick Commands

| Wave | Command |
|------|---------|
| **Wave 1 / 04-01** | `uv lock && uv run python -c "import pygit2; print(pygit2.__version__)" && uv run pytest tests/infra/code_intel/git_history/test_graveyard.py -q` |
| **Wave 2 / 04-02** | `uv run pytest tests/core/test_code_context.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py tests/benchmarks/code_intel/test_graveyard_bench.py -k "deleted or graveyard or temporal or touched_by" -q` |
| **Wave 3 / 04-03** | `uv run pytest tests/infra/code_intel/git_history/test_blame.py tests/infra/code_intel/scip/test_scip_adapter.py -q` |
| **Wave 4 / 04-04** | `uv run pytest tests/core/test_code_context.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py tests/benchmarks/code_intel/test_blame_bench.py tests/benchmarks/code_intel/test_cost_discipline.py -k "blame or churn or temporal or index_stale" -q` |

---

## Shared Execution Requirements

- [ ] Keep all public history changes on the existing `code` tool.
- [ ] Keep all git-history logic under `src/atelier/infra/code_intel/git_history/`.
- [ ] Keep `src/atelier/gateway/adapters/mcp_server.py` additive-only and `src/atelier/core/capabilities/code_context/engine.py` orchestration-only.
- [ ] Treat `tests/gateway/test_p0_mcp_surfaces.py` as mandatory in the surface-wiring waves (`04-02`, `04-04`).
- [ ] Do not claim benchmark, validation-matrix, cost-discipline, or trace closeout unless the owning wave task names the files and verification command explicitly.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Confirm hotspot containment in `mcp_server.py` and `engine.py` after Waves 2 and 4 | `HIST-01`, `HIST-02` | Tests prove behavior, not code-placement discipline | Review final diffs and confirm `mcp_server.py` only adds additive params/branches while `engine.py` only adds schema, cache, filter, freshness, and dispatch orchestration. |
| Exercise deleted-history search on a known deleted or renamed symbol after Wave 2 | `HIST-01` | Fixtures prove correctness, but operator-facing payload shape still matters | Run `code op="search"` with `scope="deleted"` for a known symbol, then repeat with `since` and `touched_by`; confirm the response keeps the normal `items` envelope and rename-aware metadata. |
| Exercise blame on a stable and a churn-heavy symbol after Wave 4 | `HIST-02` | Benchmarks do not prove the operator-facing explanation is useful | Run `code op="blame"` on one stable symbol and one recently edited symbol; confirm author, age, and churn differences are understandable without shelling out to git. |
| Confirm stale-index behavior after Wave 4 | `HIST-02` | Automated tests prove the shape, but humans must confirm the remediation hint is actionable | Reindex, create a new commit, then run `code op="blame"` on a touched symbol and confirm the response returns `index_stale` with a clear reindex hint. |

---

## Wave Trace Evidence

- `04-02` owns the M14 trace tied to `docs/plans/active/code-intel/M14-git-history.md`.
- `04-04` owns the M15 trace tied to `docs/plans/active/code-intel/M15-blame-temporal.md`.
- `04-01` and `04-03` do not promise trace closeout.

---

## Source Coverage Audit

| Source Type | Item | Covered By | Status |
|-------------|------|------------|--------|
| GOAL | Agents can reason about deleted code, renames, ownership, and stability before making changes | `04-02`, `04-04` | covered |
| REQ | `HIST-01` deleted/renamed symbol search with time-window or author filtering | `04-01`, `04-02`, `04-04` | covered |
| REQ | `HIST-02` blame and churn metadata for change-risk judgment | `04-03`, `04-04` | covered |
| RESEARCH | Keep all public changes on the existing `code` tool | all plans | covered |
| RESEARCH | Isolate git-history logic under `src/atelier/infra/code_intel/git_history/` | all plans | covered |
| RESEARCH | Keep `mcp_server.py` additive-only and `engine.py` orchestration-only | `04-02`, `04-04` + manual review | covered |
| RESEARCH | Make `pygit2` legitimacy/bootstrap explicit | `04-01` | covered |
| RESEARCH | Deleted search stays on `code op="search"` with `scope="deleted"` | `04-02` | covered |
| RESEARCH | Blame stays on `code op="blame"` | `04-04` | covered |
| RESEARCH | Propagate explicit index-vs-HEAD freshness for blame | `04-03`, `04-04` | covered |
| RESEARCH | Graveyard benchmark evidence belongs to M14 closeout | `04-02` | covered |
| RESEARCH | Blame benchmark and cost-discipline evidence belong to M15 closeout | `04-04` | covered |
| CONTEXT | No Phase 4 `CONTEXT.md` file was provided; use ROADMAP + RESEARCH defaults | all plans | covered |

---

## Validation Sign-Off

- [x] Wave 1 bootstrap/import gate completed before any public wiring
- [x] Wave 2 deleted-history search surface and M14 benchmark/trace evidence completed
- [x] Wave 3 blame/freshness substrate completed without leaking surface work
- [x] Wave 4 blame surface, benchmark, cost-discipline, and M15 trace evidence completed
- [ ] Manual hotspot and stale-index checks recorded

**Approval:** pending human/UAT sign-off after automated verification
