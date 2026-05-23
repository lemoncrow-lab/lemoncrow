# Roadmap: Atelier Code Intelligence

## Shipped Milestones

- **v1.0 Code Intelligence** ✅ — 7 phases, 21 plans, 18 requirements, shipped 2026-05-23. [Archive](milestones/v1.0-ROADMAP.md)

## Active Milestones

### v1.1 Prompt Compiler

**Goal:** Make the cacheable prefix of a coding-agent prompt deterministic, large, and identical across turns so provider-side caching fires and savings are proven in traces.

**Source of truth:** `docs/plans/active/prompt-compiler/` (P0–P8)

#### Phase 8: Block Model

**Goal**: Land the typed data model the rest of the compiler builds on.
**Depends on**: Nothing (first v1.1 phase)
**Requirements**: BLOC-01, BLOC-02
**Success Criteria**:
  1. `PromptBlock`, `Stability`, `BlockKind` dataclasses exist under `core/capabilities/prompt_compilation/`
  2. `sha256`-based `version_hash` is deterministic and tiktoken-based `token_estimate` matches within 5%
  3. Unit tests pass; validation-matrix row M-P0 added

Plans:
- [ ] 08-01-PLAN.md — `PromptBlock`, `Stability`, `BlockKind`, hashing, token estimator (P0)

#### Phase 9: Compiler Core

**Goal**: Produce a deterministic, cache-safe `CompiledPrompt` from a list of `PromptBlock`s.
**Depends on**: Phase 8
**Requirements**: COMP-01, COMP-02
**Success Criteria**:
  1. `compile_prompt(blocks)` returns `CompiledPrompt` with consistent prefix boundary across identical inputs
  2. With `tail_budget_tokens`, oversize turn/volatile blocks are knapsack-packed without touching the stable prefix
  3. Benchmark shows identical stable prefix hash across 3 repeated compile calls

Plans:
- [ ] 09-01-PLAN.md — `compile_prompt()`, `STABILITY_ORDER` sort, prefix-boundary computation, `PromptCompilerCapability` (P1)

#### Phase 10: Cache-Safety Linter

**Goal**: Catch the cache breakers that destroy prefix caching in the wild with actionable diagnostics.
**Depends on**: Phase 9
**Requirements**: LINT-01, LINT-02
**Success Criteria**:
  1. Linter detects volatile-before-stable, reordered tool schemas, timestamps/request-IDs in prefix, undersized prefix
  2. Output includes severity, rule name, and remediation hint (not just pass/fail)
  3. `lint()` is callable standalone before `compile()` for CI integration

Plans:
- [ ] 10-01-PLAN.md — `linter.py`, `lint_rules/` (ordering, tools, content, size), `LintFinding` model (P2)

#### Phase 11: Provider Adapters

**Goal**: Render a `CompiledPrompt` into each provider's shaped request body so prefix caching actually fires.
**Depends on**: Phase 9
**Requirements**: PROV-01, PROV-02, PROV-03
**Success Criteria**:
  1. OpenAI renderer emits correct `prompt_cache_key` header value
  2. Anthropic renderer inserts `cache_control: {"type": "ephemeral"}` at the exact prefix boundary
  3. Gemini and DeepSeek renderers are pure functions over `CompiledPrompt` with unit tests

Plans:
- [ ] 11-01-PLAN.md — `providers_openai.py`, `providers_anthropic.py`, `providers_gemini.py`, `providers_deepseek.py` (P3)

#### Phase 12: Trace & Telemetry Integration

**Goal**: Every `compile()` call records proof that caching is firing.
**Depends on**: Phase 9, Phase 11
**Requirements**: TRAC-01, TRAC-02
**Success Criteria**:
  1. `prompt_compilations` table persists `stable_prefix_tokens`, `dynamic_tail_tokens`, `cache_lint_score`, `stable_prefix_hash`, cache-breaker list
  2. Traces include estimated USD savings from `cache_read_tokens × cached_input_price`
  3. Scorecard rows for the compiler metrics visible in existing telemetry surface

Plans:
- [ ] 12-01-PLAN.md — `trace.py`, `prompt_compile` telemetry event, `00XX_prompt_compilations.sql` migration (P5)

#### Phase 13: CLI Surface

**Goal**: Ship `atelier prompt …` as the first user-visible surface.
**Depends on**: Phase 10, Phase 11, Phase 12
**Requirements**: CLI-01, CLI-02, CLI-03
**Success Criteria**:
  1. `atelier prompt compile <blocks.json>` renders a prompt and exits 0/1 based on lint verdict
  2. `atelier prompt lint <blocks.json>` shows rule violations with text or JSON output
  3. `atelier prompt inspect-session <PATH>` stub exists with `--help` (full body wired in Phase 14)

Plans:
- [ ] 13-01-PLAN.md — `cli_prompt.py`, extend `cli.py` with `prompt` group (P4)

#### Phase 14: Session Inspector

**Goal**: Replay existing coding-agent sessions and diagnose exactly why prefix caching isn't firing.
**Depends on**: Phase 10, Phase 12
**Requirements**: INSP-01
**Success Criteria**:
  1. Claude Code JSONL and Codex session files can be replayed through the compiler
  2. Diagnosis output lists each cache-breaker with cost impact, ordered by severity
  3. `atelier prompt inspect-session` command fully wired (stubs from Phase 13 implemented)

Plans:
- [ ] 14-01-PLAN.md — `session_importers/`, `diagnostics.py`, wire `inspect-session` CLI command (P6)

#### Phase 15: MCP Tool Integration

**Goal**: Let agents invoke the compiler in one round-trip without the Python SDK.
**Depends on**: Phase 9, Phase 11
**Requirements**: MCP-01
**Success Criteria**:
  1. MCP clients can call the compiler and receive a compiled prompt + cache metadata
  2. ADR-002 decision on `compact` extension vs new `prompt` tool recorded in `docs/decisions/`
  3. MCP surface follows `grounding.md` — no top-level tool added unless P7 explicitly decides to

Plans:
- [ ] 15-01-PLAN.md — MCP handler for compiler, ADR-002 (P7)

#### Phase 16: Python SDK

**Goal**: Give custom coding agents a stable Python surface without MCP dependency.
**Depends on**: Phase 9, Phase 11, Phase 12
**Requirements**: SDK-01
**Success Criteria**:
  1. `from atelier.prompt_compiler import PromptCompilerCapability, PromptBlock, Stability` works
  2. `compile()`, `lint()`, `render()`, `attach_usage()` callable with no MCP server running
  3. SDK stays in the monorepo (`atelier.prompt_compiler` re-exports); split-package decision recorded in ADR-002

Plans:
- [ ] 16-01-PLAN.md — `atelier/prompt_compiler/__init__.py` public surface, SDK examples (P8)

## Progress

| Phase | Plans | Status | Completed |
|-------|-------|--------|-----------|
| 8. Block Model | 0/1 | Not started | — |
| 9. Compiler Core | 0/1 | Not started | — |
| 10. Cache-Safety Linter | 0/1 | Not started | — |
| 11. Provider Adapters | 0/1 | Not started | — |
| 12. Trace & Telemetry | 0/1 | Not started | — |
| 13. CLI Surface | 0/1 | Not started | — |
| 14. Session Inspector | 0/1 | Not started | — |
| 15. MCP Tool | 0/1 | Not started | — |
| 16. Python SDK | 0/1 | Not started | — |



## Backlog

- DEFR-01: Broader cross-language/runtime edges (JNI, Rust FFI, runtime traces)
- DEFR-02: Build-system dependency graphs (Bazel/Buck) as first-class code-intel edges

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

- [x] 01-03-PLAN.md — Harden existing `code op="search"` params, ranking, snippets, provenance, and validation evidence (M2)

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
**Wave 1**

- [x] 02-01-PLAN.md — Add `code op="pattern"` via ast-grep on the existing `code` surface with explicit binary handling, budget-safe payloads, and benchmark evidence (M5)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 02-02-PLAN.md — Freeze cache, budget, defaults, and diagnostics across current code-intel flows, but keep M12 marked partial until Plans 03 and 04 complete follow-through validation (M12 core freeze)

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 02-03-PLAN.md — Add `kind="symbol"` edits through a new core seam behind the existing `edit` tool and complete the edit-side M12 follow-through checks (M4)

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 02-04-PLAN.md — Add `code op="usages"` routed reference navigation, benchmark it against grep/read, and close the remaining M12 follow-through validation (M3)

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

- [x] 03-01: Function-level embeddings and hybrid ranking inside symbol search (M6)
- [x] 03-02: Symbol-linked recall bundle on existing memory/code surfaces (M7)
- [x] 03-03: Caller and callee traversal from the SCIP call graph (M8)

### Phase 4: Historical Code Intelligence

**Goal**: Agents can reason about deleted code, renames, ownership, and stability before making changes.
**Depends on**: Phase 3
**Requirements**: HIST-01, HIST-02
**Success Criteria** (what must be TRUE):

  1. Agent can search deleted or renamed symbols and filter historical results by time window or author.
  2. Agent can inspect blame and churn metadata for a symbol to judge ownership and stability before editing.

**Plans**: 4 plans

Plans:
**Wave 1**

- [x] 04-01-PLAN.md — Pin and bootstrap `pygit2`, then build the isolated git-history graveyard substrate with real infra tests only

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 04-02-PLAN.md — Wire deleted-history search on the existing `code` surface, add graveyard benchmark evidence, and close explicit M14 trace ownership

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 04-03-PLAN.md — Build blame/churn substrate and explicit freshness metadata propagation with infra tests only

**Wave 4** *(blocked on Wave 2 and Wave 3 completion)*

- [x] 04-04-PLAN.md — Wire `code op="blame"` and live temporal filtering, then close M15 benchmark, cost-discipline, and trace evidence

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

- [x] 05-01: Build-vs-integrate checkpoint and decision memo before scale backend work (M18)
- [x] 05-02: Validated large-repo backend routing for search workloads (M16)
- [x] 05-03: Partial cross-language edge resolution with confidence scoring (M17)

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
**Wave 1**

- [x] 06-01-PLAN.md — First-context bootstrap and pinned memory prefetch pipeline (M11)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 06-02-PLAN.md — External dependency indexing and `scope="external"` routing (M9)

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 06-03-PLAN.md — Multi-repo workspace routing and repo-aware result handling (M10)

### Phase 7: Maintainer Playbooks & Scorecards

**Goal**: Maintainers consistently choose the lowest-token code-intel path and can measure whether the new workflow is being adopted.
**Depends on**: Phase 6
**Requirements**: ENBL-02
**Success Criteria** (what must be TRUE):

  1. Maintainers have practical documentation that explains when to use `code`, `read`, `search`, and symbol-scoped edits.
  2. Maintainers can inspect validation guidance and scorecard metrics that show cache usage, symbol-first adoption, and token-cost outcomes for the shipped code-intel flows.

**Plans**: 1 plan

Plans:

- [ ] 07-01-PLAN.md — Agent-OS playbooks, validation matrix updates, ADR acceptance, and scorecard metrics (M13)

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5 → 6 → 7

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Retrieval Core & Routed Symbol Search | 3/3 | Complete   | 2026-05-18 |
| 2. Structural Discovery & Symbol-Safe Change Flows | 4/4 | Complete | 2026-05-19 |
| 3. Semantic Recall & Relationship Navigation | 3/3 | Complete   | 2026-05-19 |
| 4. Historical Code Intelligence | 4/4 | Complete | 2026-05-19 |
| 5. Scale Decision & Extended Retrieval Reach | 3/3 | Complete   | 2026-05-19 |
| 6. Bootstrap, Dependency Scope & Multi-Repo Workspaces | 3/3 | Complete | 2026-05-23 |
| 7. Maintainer Playbooks & Scorecards | 0/1 | Not started | - |
