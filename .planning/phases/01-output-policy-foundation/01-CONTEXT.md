# Phase 1: Output Policy Foundation - Context

**Gathered:** 2026-05-23
**Status:** Ready for planning

<domain>
## Phase Boundary

This phase defines and wires a shared output-policy foundation for code/search/context renderers so compact defaults and hard truncation are consistently enforced. It does not rewrite retrieval quality logic or add unrelated MCP capabilities.

</domain>

<decisions>
## Implementation Decisions

### Output Cap Policy
- **D-01:** Lock compact baseline caps as the default profile: search `1800`, relation `2200`, context `6500`, node outline `3000`, node code `2500` (char-level caps).
- **D-02:** Explicit `budget_tokens` may override default policy budgets, but outputs must still obey operation-level hard safety caps.

### Scope Discipline
- **D-03:** Token reduction must come from output shaping/rendering discipline, not from reducing internal retrieval depth or weakening recall.

### the agent's Discretion
- Choose exact policy object/module placement and naming conventions consistent with current codebase patterns.
- Choose deterministic truncation boundary behavior (newline-aware cut vs direct cut), as long as safety caps are always enforced.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone and phase scope
- `.planning/PROJECT.md` — milestone goals, constraints, and out-of-scope boundaries.
- `.planning/REQUIREMENTS.md` — Phase 1 requirements (`OUT-01`, `OUT-02`, `OUT-03`) and traceability.
- `.planning/ROADMAP.md` — Phase 1 goal and success criteria.

### Existing implementation surfaces
- `src/atelier/core/capabilities/code_context/engine.py` — current renderer and response-shaping paths for code/context operations.
- `src/atelier/core/capabilities/code_context/models.py` — typed response model layer for code-context payloads.
- `src/atelier/gateway/adapters/mcp_server.py` — MCP tool input/output dispatch and parameter surface for `code` operations.

### Validation references
- `tests/core/test_code_context.py` — behavior and regression coverage for code-context operations.
- `tests/benchmarks/test_code_search_ab_real.py` — benchmark fixture for code-search token/recall/latency comparisons.
- `tests/benchmarks/test_code_explore_ab_real.py` — benchmark fixture for explore payload behavior.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `code_context.engine` helper flows already centralize most operation response assembly; they are the best insertion point for shared output-policy enforcement.
- Existing budget and packing utilities under `src/atelier/core/capabilities/code_context/` can be reused instead of introducing parallel truncation logic.

### Established Patterns
- MCP gateway remains a thin dispatcher; capability/runtime modules hold behavior logic.
- Tests are operation-focused (`test_code_context.py`) and benchmark fixtures in `tests/benchmarks/` are already used for token/latency evidence.

### Integration Points
- `tool_code` dispatch in `mcp_server.py` for new flags/default behavior exposure.
- `engine.py` renderers for search/relation/context/node-style outputs.
- benchmark matrix generation/tests for enforcing new token targets without recall regression.

</code_context>

<specifics>
## Specific Ideas

- Phase 1 should establish shared policy primitives and hard-cap helpers first, before reworking individual operation renderers in later phases.
- Budget override behavior must remain predictable: user-provided budget affects output size, but no operation may bypass safety ceilings.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 1-Output Policy Foundation*
*Context gathered: 2026-05-23*
