---
phase: 6
slug: bootstrap-dependency-scope-multi-repo-workspaces
status: planned
created: 2026-05-19
source: phase-planning
---

# Phase 6 - Validation Strategy

> Per-phase validation contract for first-context bootstrap, external dependency scope routing, and multi-repo workspace code-intel routing.

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
| **Wave 1** | `06-01` | Establish the bootstrap helper and worker path first because it is self-contained and keeps first-context warming on existing seams. |
| **Wave 2** | `06-02` | Add external dependency scope after the bootstrap path is in place; this introduces origin metadata and explicit scope behavior without multi-repo fan-out yet. |
| **Wave 3** | `06-03` | Land multi-repo workspace routing last because it depends on the new repo/origin result metadata and shares gateway/model files with earlier plans. |

---

## Sampling Rate

- **After every task commit:** run the smallest targeted pytest subset for the task's owned files only.
- **After every wave:** run that wave's quick command only.
- **Before final phase verification:** run `make lint && make typecheck && make test`, tracking unrelated pre-existing failures separately.
- **Max feedback latency:** 300 seconds.

---

## Per-Plan Verification Map

| Plan | Milestone | Requirement | Secure / correct behavior | Expected automated coverage |
|------|-----------|-------------|---------------------------|-----------------------------|
| `06-01` | M11 closeout | `ENBL-01` | first `context` entry enqueues bootstrap work on the existing worker path, persists deterministic pinned memory, and reuses warm state on later sessions | `tests/core/service/test_bootstrap_context.py`, `tests/gateway/test_mcp_tool_handlers.py`, `tests/benchmarks/code_intel/test_bootstrap_prefetch_bench.py` |
| `06-02` | M9 closeout | `DISC-05` | `scope="external"` is explicit, dependency hits are tagged with origin metadata, and symbol-edit rejects external targets clearly | `tests/infra/code_intel/scip/test_scip_adapter.py`, `tests/core/test_code_context.py`, `tests/gateway/test_mcp_tool_handlers.py`, `tests/benchmarks/code_intel/test_external_scope_bench.py` |
| `06-03` | M10 closeout | `NAVG-04` | supported `code` ops can fan out across `.atelier/workspace.toml` repos, return repo-aware results, and narrow by `repo` without changing hashed `repo_id` | `tests/core/test_code_context_workspace.py`, `tests/gateway/test_mcp_tool_handlers.py`, `tests/gateway/test_p0_mcp_surfaces.py`, `tests/benchmarks/code_intel/test_workspace_bench.py` |

---

## Wave-Specific Quick Commands

| Wave | Command |
|------|---------|
| **Wave 1 / 06-01** | `uv run pytest tests/core/service/test_bootstrap_context.py tests/gateway/test_mcp_tool_handlers.py tests/benchmarks/code_intel/test_bootstrap_prefetch_bench.py -k "bootstrap or get_context" -q` |
| **Wave 2 / 06-02** | `uv run pytest tests/infra/code_intel/scip/test_scip_adapter.py tests/core/test_code_context.py tests/gateway/test_mcp_tool_handlers.py tests/benchmarks/code_intel/test_external_scope_bench.py -k "external or origin or scope" -q` |
| **Wave 3 / 06-03** | `uv run pytest tests/core/test_code_context_workspace.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py tests/benchmarks/code_intel/test_workspace_bench.py -k "workspace or repo_name or repo_filter" -q` |

---

## Shared Execution Requirements

- [ ] Keep bootstrap on the existing `context` + worker path; no new top-level MCP tool.
- [ ] Keep bootstrap deterministic and current-repo-only; do not add LLM-generated symbol summaries in Phase 6.
- [ ] Support external dependency artifacts when present; do not require real external SCIP generation in this phase.
- [ ] Preserve the existing hashed `repo_id` cache and storage layout.
- [ ] Keep `smart_search` and the Zoekt runtime search-only and out of Phase 6 workspace routing.
- [ ] Keep `engine.py` and `mcp_server.py` thin by pushing new logic into helper modules.
- [ ] Treat broad repo failures outside these targeted suites as informational unless directly caused by Phase 6 changes.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Confirm warm second-session context reuse after `06-01` | `ENBL-01` | Tests prove state reuse, but humans should confirm the warmed context feels useful | Call `context` twice against a fixture repo and confirm the second run exposes the pinned bootstrap content without a duplicate bootstrap job. |
| Confirm external dependency payload clarity after `06-02` | `DISC-05` | Automated tests prove behavior, but a human should confirm origin tagging is understandable | Query a known dependency symbol with `scope="external"` and confirm the payload is clearly marked external and edit rejection is actionable. |
| Confirm multi-repo disambiguation after `06-03` | `NAVG-04` | Tests prove routing, but humans should inspect the repo-aware payload shape | Run one workspace search with no filter and one with `repo="..."`; confirm `repo_name` disambiguates otherwise similar hits. |

---

## Wave Trace Evidence

- `06-01` owns the M11 trace tied to `docs/plans/active/code-intel/M11-bootstrap.md`.
- `06-02` owns the M9 trace tied to `docs/plans/active/code-intel/M9-external-deps.md`.
- `06-03` owns the M10 trace tied to `docs/plans/active/code-intel/M10-multi-repo.md`.

---

## Source Coverage Audit

| Source Type | Item | Covered By | Status |
|-------------|------|------------|--------|
| GOAL | Agents start with warmed code-intel context and can route searches across dependency and workspace boundaries | `06-01`, `06-02`, `06-03` | covered |
| REQ | `ENBL-01` first-context bootstrap and prefetch behavior | `06-01` | covered |
| REQ | `DISC-05` distinguish external dependency symbols from workspace symbols | `06-02` | covered |
| REQ | `NAVG-04` search and resolve code intelligence across supported multi-repo workspaces with repo-aware results | `06-03` | covered |
| RESEARCH | keep Phase 6 as three plans aligned to M11, M9, and M10 | `06-01`, `06-02`, `06-03` | covered |
| RESEARCH | no new top-level MCP tool | all plans | covered |
| RESEARCH | bootstrap stays on existing `context` plus worker path | `06-01` | covered |
| RESEARCH | support external artifacts when present; do not require real external SCIP generation | `06-02` | covered |
| RESEARCH | do not migrate hashed `repo_id` cache/storage layout | `06-02`, `06-03` | covered |
| RESEARCH | keep smart_search and Zoekt search-only runtime out of scope | `06-02`, `06-03` | covered |
| RESEARCH | keep `engine.py` and `mcp_server.py` thin | all plans | covered |
| RESEARCH | keep multi-repo behavior on `code` operations only and read-only in this phase | `06-03` | covered |
| CONTEXT | No Phase 6 `CONTEXT.md` file was provided; apply the direct user constraints in this planning request as binding planning context | all plans | covered |

---

## Validation Sign-Off

- [x] Wave 1 M11 bootstrap implementation completed with targeted tests and benchmark smoke
- [x] Wave 2 M9 external-scope implementation completed with targeted tests and benchmark smoke
- [x] Wave 3 M10 workspace routing completed with targeted tests and benchmark smoke
- [ ] Manual warm-context, origin-tagging, and repo-disambiguation checks recorded

**Approval:** pending human/UAT sign-off after `06-VERIFICATION.md`
