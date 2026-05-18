# Roadmap: Atelier Code Intelligence

## Overview

This brownfield roadmap extends Atelier's existing CLI, MCP, HTTP, and frontend-backed runtime with the full M0-M18 code-intelligence program. The phases follow the active milestone dependency order, keep all work on existing tool surfaces by default, and drive toward near-zero-token code search, navigation, and editing with observable cache, provenance, history, scale, and enablement outcomes.

## Phases

**Phase Numbering:**

- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Retrieval Core & Routed Symbol Search** - Establish shared cache/budget primitives, routed symbol backends, and hardened symbol lookup on existing `code` ops.
- [ ] **Phase 2: Structural Discovery & Symbol-Safe Change Flows** - Add structural pattern search, freeze low-token defaults, and ship symbol edits plus usages.
- [ ] **Phase 3: Semantic Recall & Relationship Navigation** - Layer semantic retrieval, symbol-linked memory, and caller/callee graph traversal.
- [ ] **Phase 4: Historical Code Intelligence** - Make deleted symbols, renames, blame, and churn first-class code-intel queries.
- [ ] **Phase 5: Scale Decision & Extended Retrieval Reach** - Gate large-repo backend work, then ship validated scale routing and cross-language edges.
- [ ] **Phase 6: Bootstrap, Dependency Scope & Multi-Repo Workspaces** - Warm code-intel state on first context and expand routing across external deps and repo boundaries.
- [ ] **Phase 7: Maintainer Playbooks & Scorecards** - Document symbol-first usage and publish the scorecard/validation guidance that preserves the token wins.

## Phase Details

### Phase 1: Retrieval Core & Routed Symbol Search

**Goal**: Agents can retrieve symbols through existing `code` operations with cache-aware, provenance-aware, budget-packed defaults.
**Depends on**: Nothing (first phase)
**Requirements**: FNDN-01, FNDN-02, NAVG-01
**Success Criteria** (what must be TRUE):

  1. Agent can repeat the same `code` lookup and receive `cache_hit`, `tokens_saved`, and provenance metadata in the response.
  2. Agent can query symbol intelligence through the existing `code` surface and get routed SCIP-backed results when an index is available without breaking fallback behavior.
  3. Agent can use `code op="search"` to get ranked, outline-first symbol hits with hardened defaults instead of starting with ad hoc text search.

**Plans**: 3 plans

Plans:
**Wave 1**

- [x] 01-01-PLAN.md — Complete/harden shared retrieval cache, budget packing, and the benchmark harness gap inside `code_context` (M0)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 01-02-PLAN.md — Route SCIP-backed symbol lookup through `SymbolIntelStore` with safe fallback on the existing `code` surface (M1)

**Wave 3** *(blocked on Wave 2 completion)*

- [ ] 01-03-PLAN.md — Harden existing `code op="search"` params, ranking, snippets, provenance, and validation evidence (M2)

### Phase 2: Structural Discovery & Symbol-Safe Change Flows

**Goal**: Agents can find code by structure, inspect symbol usages, and apply named-symbol edits without line-number workflows.
**Depends on**: Phase 1
**Requirements**: DISC-01, DISC-02, NAVG-02
**Success Criteria** (what must be TRUE):

  1. Agent can run `code op="pattern"` to find structural matches and preview or apply AST-aware rewrites instead of regex-only search.
  2. Agent can submit `edit` requests with `kind="symbol"` and update the intended named symbol atomically, while ambiguous targets are rejected clearly.
  3. Agent can call `code op="usages"` and get grouped symbol references without falling back to ad hoc text search by default.

**Plans**: 4 plans

Plans:

- [ ] 02-01: Structural pattern search and rewrite via ast-grep on `code op="pattern"` (M5)
- [ ] 02-02: Outline-first audit, cache hardening, and budget-policy freeze across code-intel surfaces (M12)
- [ ] 02-03: Rich symbol-scoped edits on the existing `edit` tool (M4)
- [ ] 02-04: `code op="usages"` symbol reference navigation (M3)

### Phase 3: Semantic Recall & Relationship Navigation

**Goal**: Agents can recover intent, prior context, and symbol relationships before they change code.
**Depends on**: Phase 2
**Requirements**: DISC-03, DISC-04, NAVG-03
**Success Criteria** (what must be TRUE):

  1. Agent can use semantic or hybrid symbol search to find relevant functions when the exact symbol name is unknown.
  2. Agent can recall symbol-linked memory through the existing memory/code surfaces and recover prior context with low token overhead.
  3. Agent can inspect callers and callees for a symbol through the existing `code` surface.

**Plans**: 3 plans

Plans:

- [ ] 03-01: Function-level embeddings and hybrid ranking inside symbol search (M6)
- [ ] 03-02: Symbol-linked recall bundle on existing memory/code surfaces (M7)
- [ ] 03-03: Caller and callee traversal from the SCIP call graph (M8)

### Phase 4: Historical Code Intelligence

**Goal**: Agents can reason about deleted code, renames, ownership, and stability before making changes.
**Depends on**: Phase 3
**Requirements**: HIST-01, HIST-02
**Success Criteria** (what must be TRUE):

  1. Agent can search deleted or renamed symbols and filter historical results by time window or author.
  2. Agent can inspect blame and churn metadata for a symbol to judge ownership and stability before editing.

**Plans**: 2 plans

Plans:

- [ ] 04-01: Git-history graveyard for deleted symbols, renames, and temporal filters (M14)
- [ ] 04-02: Blame and churn annotations on `code op="blame"` plus live temporal filtering (M15)

### Phase 5: Scale Decision & Extended Retrieval Reach

**Goal**: Atelier can make the scale-backend choice explicitly and then extend code intelligence to large repos and supported cross-language edges.
**Depends on**: Phase 4
**Requirements**: ENBL-03, SCAL-01, SCAL-02
**Success Criteria** (what must be TRUE):

  1. Maintainers have a documented build-vs-integrate decision record before large-repo backend work proceeds.
  2. Agent can route large-repo search workloads through the validated scale backend and see which backend served the result.
  3. Agent can see supported cross-language references with confidence scoring on symbol and usage results for the planned Python/C, subprocess, and dynamic-import cases.

**Plans**: 3 plans

Plans:

- [ ] 05-01: Build-vs-integrate checkpoint and decision memo before scale backend work (M18)
- [ ] 05-02: Validated large-repo backend routing for search workloads (M16)
- [ ] 05-03: Partial cross-language edge resolution with confidence scoring (M17)

### Phase 6: Bootstrap, Dependency Scope & Multi-Repo Workspaces

**Goal**: Agents start with warmed code-intel context and can route searches across dependency and workspace boundaries.
**Depends on**: Phase 5
**Requirements**: ENBL-01, DISC-05, NAVG-04
**Success Criteria** (what must be TRUE):

  1. First workspace context bootstraps and prefetches the most relevant code-intel state so later retrieval-heavy sessions start warm.
  2. Agent can distinguish external dependency symbols from workspace symbols in results, and symbol-edit flows reject external targets cleanly.
  3. Agent can search and resolve code intelligence across supported multi-repo workspaces with repo-aware results and filters.

**Plans**: 3 plans

Plans:

- [ ] 06-01: First-context bootstrap and pinned memory prefetch pipeline (M11)
- [ ] 06-02: External dependency indexing and `scope="external"` routing (M9)
- [ ] 06-03: Multi-repo workspace routing and repo-aware result handling (M10)

### Phase 7: Maintainer Playbooks & Scorecards

**Goal**: Maintainers consistently choose the lowest-token code-intel path and can measure whether the new workflow is being adopted.
**Depends on**: Phase 6
**Requirements**: ENBL-02
**Success Criteria** (what must be TRUE):

  1. Maintainers have practical documentation that explains when to use `code`, `read`, `search`, and symbol-scoped edits.
  2. Maintainers can inspect validation guidance and scorecard metrics that show cache usage, symbol-first adoption, and token-cost outcomes for the shipped code-intel flows.

**Plans**: 1 plan

Plans:

- [ ] 07-01: Agent-OS playbooks, validation matrix updates, ADR acceptance, and scorecard metrics (M13)

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5 → 6 → 7

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Retrieval Core & Routed Symbol Search | 2/3 | In Progress|  |
| 2. Structural Discovery & Symbol-Safe Change Flows | 0/4 | Not started | - |
| 3. Semantic Recall & Relationship Navigation | 0/3 | Not started | - |
| 4. Historical Code Intelligence | 0/2 | Not started | - |
| 5. Scale Decision & Extended Retrieval Reach | 0/3 | Not started | - |
| 6. Bootstrap, Dependency Scope & Multi-Repo Workspaces | 0/3 | Not started | - |
| 7. Maintainer Playbooks & Scorecards | 0/1 | Not started | - |
