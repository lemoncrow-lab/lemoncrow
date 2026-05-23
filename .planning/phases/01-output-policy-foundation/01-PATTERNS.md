# Phase 1: Output Policy Foundation - Pattern Map

**Mapped:** 2026-05-23  
**Files analyzed:** 6  
**Analogs found:** 6 / 6

## File Classification

| Planned File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `src/atelier/core/capabilities/code_context/engine.py` | service | request-response | same file | exact |
| `src/atelier/core/capabilities/code_context/models.py` | model | transform | same file | exact |
| `src/atelier/core/capabilities/code_context/budget.py` | utility | transform | same file | exact |
| `src/atelier/gateway/adapters/mcp_server.py` | route | request-response | same file | exact |
| `tests/core/test_code_context.py` | test | request-response | same file | exact |
| `tests/benchmarks/test_code_search_ab_real.py` | test | batch | same file | exact |

## Pattern Assignments

### Engine-level response shaping
- Keep behavior implementation in `src/atelier/core/capabilities/code_context/engine.py`.
- Add shared policy integration to existing tool paths instead of introducing parallel render stacks.

### Model extension discipline
- Extend typed models in `src/atelier/core/capabilities/code_context/models.py` where needed.
- Prefer additive fields and preserve existing contracts.

### Budget and truncation helper reuse
- Reuse packing and budget utilities under `src/atelier/core/capabilities/code_context/` rather than duplicating cap logic in gateway code.

### Gateway thin-dispatch pattern
- Keep `src/atelier/gateway/adapters/mcp_server.py` focused on op dispatch + argument validation.
- Avoid embedding renderer logic in gateway handlers.

### Test-first regression lock
- Add/adjust operation-level assertions in `tests/core/test_code_context.py` for defaults, safety caps, and precedence behavior.
- Keep benchmark checks in `tests/benchmarks/` for token/latency evidence.

## Shared Constraints

- No new top-level MCP tools.
- Compact defaults remain on unless explicitly requested otherwise.
- `budget_tokens` may influence output size, but hard safety caps are always final.
