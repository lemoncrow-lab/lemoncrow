# Phase 3 Research: Context Compact Mode

**Researched:** 2026-05-23  
**Phase:** 3 - Context Compact Mode

## Objective

Plan a deterministic compact context renderer that preserves decision usefulness while enforcing strict bounded output shape.

## Source Inputs

- `.planning/ROADMAP.md`
- `.planning/REQUIREMENTS.md`
- `.planning/STATE.md`
- `.planning/phases/01-output-policy-foundation/01-CONTEXT.md`
- `.planning/phases/02-search-relation-compact-rendering/02-01-SUMMARY.md`
- `src/atelier/core/capabilities/code_context/engine.py`
- `tests/core/test_code_context.py`

## Key Findings

1. Context payloads already flow through `tool_context` + `_pack_single_payload`; this is the primary integration point.
2. Deterministic caps should be enforced at three layers: entry-point count, related symbol count/per-file cap, and code-block count/size.
3. Import/export noise suppression belongs in symbol/related filtering before rendering, not in final truncation only.
4. Existing tests already exercise context budget behavior; Phase 3 should add assertions for structure and per-section caps.

## Planning Guidance

- Reuse Phase 1 policy primitives and avoid bespoke cap logic.
- Keep context shape stable (`entry_points`, `related`, `code`) while constraining payload volume.
- Add regression checks for cap ordering so future changes cannot reintroduce oversized context blocks.
