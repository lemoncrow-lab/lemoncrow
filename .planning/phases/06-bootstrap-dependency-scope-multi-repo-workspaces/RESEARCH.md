# Phase 6 Research — Bootstrap, Dependency Scope & Multi-Repo Workspaces

**Date:** 2026-05-19
**Phase:** 06-bootstrap-dependency-scope-multi-repo-workspaces
**Requirements:** `ENBL-01`, `DISC-05`, `NAVG-04`

## Summary

Phase 6 should stay aligned with the roadmap as three plans:

- `06-01` — M11 first-context bootstrap
- `06-02` — M9 external dependency indexing and `scope="external"`
- `06-03` — M10 multi-repo workspaces

The main repo-specific finding is that the milestone docs describe cleaner
surfaces than the live repo currently has. Phase 6 should therefore stay on
existing entry points and add the smallest possible wrappers instead of trying
to make `CodeContextEngine` or `mcp_server.py` own all new concerns directly.

## Recommended decomposition

### 06-01 — M11 first-context bootstrap

- Keep this plan on the existing `context` path and the current worker loop.
- Trigger bootstrap from `tool_get_context` and reuse the existing worker tick
  pattern instead of adding a new top-level MCP tool or scheduler.
- Introduce a new worker job type plus a deterministic bootstrap payload writer
  for pinned memory blocks tagged under `bootstrap/<repo_id>/...`.
- Keep Phase 6 bootstrap **implicit, async, current-repo-only, and
  deterministic**.
- Do **not** add LLM-generated symbol summaries in this phase; that is the
  biggest avoidable cost/risk spike in the milestone stub.

### 06-02 — M9 external dependency indexing and `scope="external"`

- Treat this as explicit external-artifact plumbing, not a broad search/router
  rewrite.
- Keep using the existing SCIP cache root and discover `external-*.scip`
  artifacts alongside repo-local artifacts when they exist.
- Extend `SymbolRecord` and returned symbol-shaped payloads with additive
  `origin: "internal" | "external"` metadata.
- Keep `scope="repo"` as the default; `scope="external"` is additive and
  explicit.
- Reject symbol edits on external results in `symbol_edit.py`, where symbol edit
  target validation already lives.
- Strong default: support external artifacts when present; do **not** require
  real external SCIP generation in this phase because the local environment does
  not ship the needed `scip-*` binaries.

### 06-03 — M10 multi-repo workspaces

- Add a new workspace config/parser layer instead of making
  `CodeContextEngine` itself multi-root.
- The milestone doc assumes `tool_code` already has a `repo` param, but the
  live MCP surface still centers on `repo_root`; planning must account for that
  gap explicitly.
- Keep the existing hashed `repo_id` as the storage/cache identity. Do **not**
  migrate cache layout to repo-name directories in Phase 6.
- Safest default: introduce `.atelier/workspace.toml`, parse it in a new helper,
  and fan out to one engine/store per repo, then merge/filter results above that
  layer.
- Keep multi-repo work read-only for now; do not widen `edit`, `read`, or
  `smart_search` into cross-repo semantics in this phase.

## Reusable seams

| Seam | Use in Phase 6 |
| --- | --- |
| `tool_get_context` + `_run_worker_tick_safe()` | Best current first-context entry point for bootstrap work |
| existing jobs/worker infrastructure | Smallest place to add async bootstrap execution |
| pinned memory blocks + memory metadata | Natural persistence path for bootstrap outputs |
| Phase 5 thin-adapter discipline | Keep `engine.py` and `mcp_server.py` narrow and additive |
| existing SCIP cache root | Safest landing zone for external dependency artifacts |
| existing hashed `repo_id` layout | Preserve cache/store compatibility instead of migrating identities |

## Concrete landing zones

### 06-01

- `src/atelier/gateway/adapters/mcp_server.py`
- `src/atelier/core/service/jobs.py`
- `src/atelier/core/service/worker.py`
- memory block persistence helpers under existing storage/memory surfaces

### 06-02

- `src/atelier/core/capabilities/code_context/models.py`
- `src/atelier/core/capabilities/code_context/intel_store.py`
- SCIP artifact discovery under current cache/index helpers
- `src/atelier/core/capabilities/tool_supervision/symbol_edit.py`

### 06-03

- new workspace config/parser helper under existing foundation/runtime surfaces
- `tool_code` additive plumbing for repo selection/filtering
- wrapper layer that fans out to one per-repo engine/store instead of mutating
  `CodeContextEngine` into a multi-root object

## Brownfield constraints

- `engine.py` is already a hotspot; do not add bootstrap orchestration,
  workspace parsing, or external-artifact discovery inline.
- `mcp_server.py` must stay limited to additive params, delegation, and existing
  entry-point plumbing only.
- Keep `smart_search.py` and `src/atelier/infra/code_intel/zoekt/*` out of
  scope; Phase 5 already locked Zoekt to search-only routing with a session
  supervisor.
- Do not migrate hashed `repo_id` cache/storage layout in Phase 6.
- Do not let Phase 6 absorb broader cross-language/runtime analysis.
- Broad repo tests are still noisy; keep validation targeted first.

## Key defaults and decisions

### Bootstrap

- Default to implicit bootstrap on first `context` call.
- Keep outputs deterministic and pinned in memory blocks.
- Keep refresh policy simple in Phase 6 (record partial completion / retry
  later), not a full commit/time-driven refresh system.

### External dependencies

- Default to explicit `scope="external"` only.
- Add `origin` metadata additively instead of widening existing result shapes.
- Reject external symbol edits with a clear error instead of silently no-oping.

### Multi-repo

- Default to `.atelier/workspace.toml` plus per-repo engine/store fan-out.
- Keep `repo_name`/filtering as surface metadata, but leave underlying cache id
  on hashed absolute-path `repo_id`.
- Keep Phase 6 multi-repo behavior on `code` operations only.

## Risks

| Risk | Why it matters | Planning default |
| --- | --- | --- |
| M11 stub invites a new bootstrap MCP tool | Adds new surface area and bypasses the accepted existing-entry-point rule | Keep bootstrap on `tool_get_context` and worker jobs |
| M9 assumes real external SCIP generation | Local environment does not ship `scip-python`, `scip-typescript`, or `scip-go` | Support external artifacts when present; do not make binary bootstrap the plan core |
| M10 doc assumes `repo` already exists on `tool_code` | Live MCP surface still centers on `repo_root` | Treat repo filtering as additive plumbing work in 06-03 |
| Cache identity migration | Current stores/tests depend on hashed `repo_id` layout | Preserve the current identity scheme in Phase 6 |
| Hotspot sprawl into `engine.py` / `mcp_server.py` | Brownfield risk and prior phases explicitly constrained it | Push heavy logic into new helpers/wrappers and keep public files thin |
| Search-stack/workspace scope bleed | Phase 5 already ratified search-only Zoekt ownership | Keep Phase 6 focused on code-intel/bootstrap/workspace seams, not `smart_search` or Zoekt routing |

## Validation strategy

### 06-01

- targeted context + worker + memory block tests
- prove cold start creates pinned bootstrap blocks and second session reads them
  without rerunning heavy work

### 06-02

- targeted symbol routing tests for `scope="external"`
- fixture validation that external symbols are tagged and editable symbols reject
  clearly when `origin="external"`

### 06-03

- targeted multi-repo fixture tests:
  - union search across two repos
  - repo filter narrowing
  - repo-tagged result payloads

### Shared

- baseline code-intel gate:
  `uv run pytest tests/core/test_code_context.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_tool_handlers.py -q`
- full repo gate before final closeout:
  `make lint && make typecheck && make test`

## Planning notes

- `06-01` should be the first plan because Phase 6's bootstrap path can reuse
  existing single-repo assumptions safely.
- `06-02` should land before `06-03` because external-origin tagging and
  symbol-shape metadata are lower-risk than multi-repo fan-out.
- `06-03` should explicitly avoid changing `smart_search` or Zoekt lifecycle.
- Keep human questions out unless the planner truly needs a choice on implicit
  vs explicit bootstrap triggering; otherwise default to implicit first-context
  bootstrap.
