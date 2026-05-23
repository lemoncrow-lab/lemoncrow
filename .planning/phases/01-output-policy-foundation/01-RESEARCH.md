# Phase 1 Research: Output Policy Foundation

**Researched:** 2026-05-23  
**Phase:** 1 - Output Policy Foundation  
**Requirements:** OUT-01, OUT-02, OUT-03

## Objective

Define the lowest-risk way to introduce shared output-policy enforcement and hard truncation across `code_context` surfaces while preserving current retrieval quality and MCP contracts.

## Source Inputs

- `.planning/PROJECT.md`
- `.planning/REQUIREMENTS.md`
- `.planning/ROADMAP.md`
- `.planning/STATE.md`
- `.planning/phases/01-output-policy-foundation/01-CONTEXT.md`
- `src/atelier/core/capabilities/code_context/engine.py`
- `src/atelier/core/capabilities/code_context/models.py`
- `src/atelier/gateway/adapters/mcp_server.py`
- `tests/core/test_code_context.py`
- `tests/benchmarks/test_code_search_ab_real.py`
- `tests/benchmarks/test_code_explore_ab_real.py`

## Key Findings

### 1. Main optimization surface is renderer output shaping, not retrieval

- Existing code-context flows already produce strong recall on several operations.
- The largest cost driver is response verbosity drift across operations.
- A shared output policy should be applied at render/pack boundaries, not by reducing retrieval breadth.

### 2. `engine.py` is the right integration center

- Tool handlers in `CodeContextEngine` already normalize outputs per operation and apply budget packing.
- A shared policy object + hard-cap helper can be introduced once and consumed by search/relation/context/node render paths.
- Gateway (`mcp_server.py`) should stay dispatch-only with lightweight parameter passthrough.

### 3. Hard caps must be universal and final-stage

- Truncation should run after formatting/rendering so wrapper metadata cannot re-expand payload beyond safety limits.
- Operation-level safety ceilings should always apply even when caller passes `budget_tokens`.

### 4. Backward-compatible parameter model

- Keep compact as default behavior.
- Support explicit caller intent (`budget_tokens`, optional verbosity flags) while enforcing operation safety max.
- Do not switch defaults to verbose.

### 5. Validation must prove non-regression, not just lower token counts

- Required checks include recall non-regression and capped effective token metrics.
- Existing benchmark fixtures under `tests/benchmarks/` are suitable for first-pass token and latency evidence.
- Core regression coverage belongs in `tests/core/test_code_context.py`.

## Implementation Boundaries for Planning

1. Introduce policy primitives and cap helper first (shared helper/module).
2. Wire policy into existing engine render points incrementally.
3. Add/adjust tests to validate compact defaults and cap safety.
4. Keep MCP tool surface unchanged; avoid new top-level tools.

## Risks

- Over-truncation can accidentally reduce useful context; must preserve essential identity fields.
- Inconsistent per-op integration can leave token hot-spots unresolved.
- Parameter precedence drift (`budget_tokens` vs safety max) can create surprising behavior if not codified in tests.

## Validation Architecture

- Targeted tests:
  - `uv run pytest tests/core/test_code_context.py -q`
  - `uv run pytest tests/benchmarks/test_code_search_ab_real.py tests/benchmarks/test_code_explore_ab_real.py -q`
- Broader gate for Python/runtime changes:
  - `make lint && make typecheck && make test`

## Planning Summary

Phase 1 should establish a single output-policy contract with strict hard-cap enforcement, integrate it through existing `code_context` renderer paths, and lock behavior with focused regression + benchmark checks before deeper operation-specific tuning phases.
