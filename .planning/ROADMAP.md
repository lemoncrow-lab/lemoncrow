# Roadmap: Atelier Milestone v1.1 — Code MCP v2 Parity

**Milestone:** v1.1  
**Milestone Name:** Atelier Code MCP v2 Parity  
**Requirements:** 12 mapped / 12 total  
**Coverage:** 100%

## Phases

| # | Phase | Goal | Requirements |
|---|-------|------|--------------|
| 6 | Indexed Files Surface | Add `code op="files"` with tree/flat/grouped indexed outputs and filtering controls | FILES-01, FILES-02, FILES-03 |
| 7 | Explore Context Surface | Add `code op="explore"` for grouped source + relationship output in one call | EXPL-01, EXPL-02, EXPL-03 |
| 8 | Status Surface | Add `code op="status"` for index health, cache visibility, and freshness telemetry | STAT-01, STAT-02, STAT-03 |
| 9 | Benchmark + Docs Closeout | Update MCP docs and benchmark comparisons for v2 operations and effective-token reporting | DOCS-01, BMRK-01, BMRK-02 |

## Phase Details

### Phase 6: Indexed Files Surface
**Goal:** Give agents a cheap indexed repository map before any search or symbol drill-down.

**Success criteria:**
1. `code op="files"` supports `tree`, `flat`, and `grouped` formats with deterministic output shape.
2. Filters (`path`, `pattern`, `max_depth`, `include_metadata`) operate against index-backed data.
3. Response includes budget/cache/provenance metadata and enforces compact defaults.

### Phase 7: Explore Context Surface
**Goal:** Collapse multi-step source exploration into one bounded `explore` response.

**Success criteria:**
1. `code op="explore"` returns grouped source snippets relevant to requested symbols/files.
2. Explore output includes relationship context (calls/usages) with bounded limits.
3. Explore responses stay within `budget_tokens` using deterministic truncation behavior.

### Phase 8: Status Surface
**Goal:** Expose index and cache state so agents can reason about freshness and confidence.

**Success criteria:**
1. `code op="status"` reports index health metrics (counts, backend details, readiness).
2. `code op="status"` includes cache/freshness hints useful for route/rescue decisions.
3. Status payload remains compact and host-neutral for MCP consumers.

### Phase 9: Benchmark + Docs Closeout
**Goal:** Prove and document the v2 code surfaces with measurable comparisons.

**Success criteria:**
1. `docs/sdk/mcp.md` reflects the shipped code-op surface and usage guidance.
2. Benchmark reports include Atelier vs Serena and CodeGraph-style comparison rows for relevant v2 flows.
3. Effective-token reporting remains visible in benchmark summary output.

## Notes

- Routes/autosync are explicitly deferred to post-v1.1 follow-up work.
- No new top-level MCP tools: all functionality lands via `mcp__atelier__code` op extensions.
- Completion verification: targeted phase tests passed, `benchmarks/mcp_tools/bench_code.py` passed, and 3-way comparison (`bench_code_3way.py`) executed with Atelier/Serena/code-index-mcp rows.

---
*Created: 2026-05-23*
*Last updated: 2026-05-23 after milestone v1.1 completion verification*
