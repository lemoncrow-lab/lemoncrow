# M4 — Scoped pull-context API

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).

## Goal

Provide an explicit "given this subtask description, return the minimal scoped context required to act on it" entry point. Replaces the implicit pattern of "retrieve broadly, hope the agent filters."

## Augment reference

[CIV pattern guide](https://www.augmentcode.com/guides/coordinator-implementor-verifier):

> The Coordinator uses a pull-model to assemble scoped context per subtask. Rather than slicing the full repo and handing everything to implementors, it queries the Context Engine for only the context each subtask needs.

Three isolation layers in their design:

1. **Input isolation** — only coordinator-assembled context for this subtask reaches the implementor.
2. **Output isolation** — structured contracts; only declared variables propagate forward.
3. **Filesystem isolation** — git worktrees prevent concurrent write conflicts.

M4 implements layer 1. Layers 2 and 3 are out of scope (would require full CIV decomposition; see [`grounding.md`](grounding.md) §"Not in this plan").

## Background

Atelier's current `context_reuse/capability.py` exposes scoring, BM25, dead-end filtering, and ranking — all the pieces — but no single "give me context for this subtask" entry point. Callers have to compose. The result: retrieval tends to over-fetch because there's no API gradient pushing toward tightness.

M4 adds the gradient.

## Module layout

```
src/atelier/core/capabilities/scoped_context/  (new)
  __init__.py
  capability.py        — ScopedContextCapability orchestrator
  models.py            — Subtask, ScopedContext, ContextBudget
  pull.py              — pull(subtask) → ScopedContext
  prune.py             — drop candidates that don't intersect subtask scope
```

Reuses without modification:

- `core/capabilities/code_context/engine.py` — symbol/file retrieval.
- `core/capabilities/context_reuse/bm25.py` — lexical scoring.
- `core/capabilities/context_reuse/ranking.py` — final rank.
- `core/capabilities/context_reuse/dead_ends.py` — drop known-dead patterns.
- `core/capabilities/code_context/budget.py` — token packer.

Depends on M1 commit chunks landing in `code_context.search_symbols()` candidate set.

## API

```python
@dataclass
class Subtask:
    description: str               # natural-language intent
    affected_paths: list[str] = []  # paths the subtask is allowed to touch
    keywords: list[str] = []        # explicit must-include terms
    excluded_paths: list[str] = []  # paths the subtask must not touch
    budget_tokens: int = 4000

@dataclass
class ScopedContext:
    chunks: list[ContextChunk]      # ranked, packed within budget_tokens
    rationale: str                  # why these chunks; cite the score
    excluded: list[ExclusionRecord] # what we dropped and why
    trace_id: str

class ScopedContextCapability:
    def pull(self, subtask: Subtask) -> ScopedContext: ...
```

The rationale + excluded fields are load-bearing: they make scoping debuggable. Without them, "why did the context engine pick those chunks?" is a black box.

## Pull algorithm

```
1. Seed candidates from three sources:
   a. code_context.search_symbols(subtask.description, mode=hybrid, k=50)
   b. code_context.search_symbols(" ".join(subtask.keywords), mode=lexical, k=20)
   c. for each path in subtask.affected_paths: outline + neighborhood
2. Drop candidates whose path matches excluded_paths.
3. Apply dead-end filter (context_reuse.dead_ends).
4. Re-rank with context_reuse.ranking using subtask.description as the rank query.
5. Pack within subtask.budget_tokens using code_context.budget.BudgetPacker.
   Outline first; expand bodies only for top-3 candidates.
6. Record ExclusionRecord for every dropped candidate with reason.
7. Emit trace; return ScopedContext.
```

The pull is deterministic and side-effect-free except for the trace emit. Cache key: `hash(subtask.description + affected_paths + keywords + index_version)`.

## MCP surface

Add a new op to the existing `context` MCP tool: `context op="pull" subtask="..." budget_tokens=4000`. Returns the rendered ScopedContext.

Callers (host CLIs, sub-agents, the agent itself) use this when starting a focused subtask, instead of the broad `context` retrieval that runs at session start.

## Interaction with M1

M1's commit chunks are part of the candidate set. The dead-end filter and ranking already treat them uniformly with symbol chunks (because they share the same embedding dimension). Net effect: scoped pull naturally surfaces relevant commit context when the subtask description matches.

## Validation

Tests under `tests/core/test_scoped_context/`:

- `test_pull_respects_budget.py` — output ≤ budget_tokens.
- `test_excluded_paths_honoured.py` — no chunk from `excluded_paths` in output.
- `test_rationale_cites_scores.py` — rationale string references the top candidate's score.
- `test_cache_hit.py` — second pull with identical Subtask returns cached result with `provenance="cached"`.
- `test_m1_commits_surface.py` — when description mentions a past pattern, the matching commit chunk appears in the result.

Benchmark under `tests/benchmarks/context_quality/M4_scoped.py`:

- 20 multi-file edits from this repo's history.
- Metric A (precision): % of returned chunks that the agent actually references in its edit.
- Metric B (recall): % of files the agent ended up needing that were in the pulled context.
- Target: precision ≥0.6, recall ≥0.85, vs. baseline broad retrieval (precision ~0.2, recall ~0.9).

The precision lift is the headline metric. Recall is a guardrail.

## Exit criteria

- `scoped_context` capability lands with the pull algorithm above.
- `context op="pull"` MCP op registered and documented.
- Cache key + cache hit confirmed.
- Benchmark targets hit (precision ≥0.6 lift, recall ≥0.85 floor).
- No regression in `tests/core/test_context_reuse.py` or `tests/core/test_code_context.py`.

## Open questions

- Should the subtask description be summarised before retrieval (long instructions hurt embedding match)? Lean toward yes — pre-summarise with a cheap LLM call if `len(description) > 500 chars`.
- Do we expose `subtask.budget_tokens` to the caller or auto-size based on `model_routing` tier? M4 ships caller-controlled; auto-sizing is a follow-up.
- Should `affected_paths` be required or optional? Required would force discipline; optional preserves backward-compat with current callers. M4 ships optional; revisit if precision lift underperforms.
