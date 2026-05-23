# Phase 8: Block Model — Context

**Gathered:** 2026-05-23
**Status:** Ready for planning
**Source:** PRD Express Path (docs/plans/active/prompt-compiler/P0-block-model.md)

<domain>
## Phase Boundary

Land the typed data model that the rest of the prompt compiler (P1–P8) builds on:
`PromptBlock`, `Stability`, `BlockKind`, hashing, and a token estimator.

This phase introduces the new capability directory
`src/atelier/core/capabilities/prompt_compilation/` with `__init__.py`,
`AGENT_README.md`, `models.py`, and `tokens.py`. No compilation, linting, or
provider rendering — those are P1–P3.

The block model is the foundational type contract. Every downstream milestone
depends on the invariants defined here being stable.

</domain>

<decisions>
## Implementation Decisions

### Data model

- `Stability` is a `str` enum: STATIC, SESSION, BRANCH, TURN, VOLATILE (total order)
- `BlockKind` is a `str` enum: TOOL_SCHEMA, SYSTEM, CODING_POLICY, REPO_SUMMARY,
  REASONBLOCK, FILE_SUMMARY, USER_TASK, GIT_DIFF, TOOL_RESULT, SCRATCHPAD
- `DEFAULT_STABILITY` maps every `BlockKind` to its canonical `Stability`
- `PromptBlock` is a frozen dataclass with slots: `id`, `kind`, `content`,
  `stability`, `cacheable`, `metadata`, `stability_override_reason`
- `version_hash` is a `cached_property` returning `sha256(content.encode()).hexdigest()`
- `token_estimate` is a `cached_property` delegating to `tokens.estimate_tokens(content)`

### Invariants enforced at construction

- `content` must be non-empty (raise `ValueError`)
- `id` must match `^[a-z0-9_./:-]+$` (raise `ValueError`)
- `stability` defaults to `DEFAULT_STABILITY[kind]`; if caller passes a different
  value, `stability_override_reason` is required (raise `ValueError` otherwise)
- `cacheable` defaults to `True`, but is forced to `False` when
  `stability in {TURN, VOLATILE}` — the block is non-cacheable regardless of
  the caller's intent

### Token estimator

- `tokens.py` ships `estimate_tokens(text: str, model: str | None = None) -> int`
- Primary: `tiktoken.get_encoding("cl100k_base")` — matches the pattern in
  `src/atelier/core/capabilities/repo_map/budget.py` (the canonical precedent)
- Fallback: `max(1, len(text) // 4)` when tiktoken is unavailable
- Do NOT add a new tiktoken dependency — it is already in the project's deps

### the agent's Discretion

- `__init__.py` re-export shape (use `__all__` for clean imports)
- `AGENT_README.md` content (brief docstring-style orientation for downstream agents)
- Exact `ValueError` message strings
- Whether to use `__post_init__` or `__init_subclass__` for invariant enforcement
  (`__post_init__` is idiomatic for frozen dataclasses)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Plan source
- `docs/plans/active/prompt-compiler/P0-block-model.md` — Full P0 spec (authoritative)
- `docs/plans/active/prompt-compiler/index.md` — Dependency graph + build order

### Existing patterns to reuse
- `src/atelier/core/capabilities/repo_map/budget.py` — Canonical tiktoken token counting (`count_tokens`)
- `src/atelier/core/capabilities/context_compression/scoring.py` — Existing capability module structure

### Architecture constraints
- `CLAUDE.md` — `uv run pytest`, `make lint`, `make typecheck` commands
- `docs/agent-os/validation-matrix.md` — Validation matrix (row M-P0 must be added)

</canonical_refs>

<specifics>
## Specific Ideas

- `PromptBlock` must be `frozen=True, slots=True` for immutability and performance
- `version_hash` and `token_estimate` use `cached_property` — note that `cached_property`
  doesn't work on frozen dataclasses directly; use `object.__setattr__` trick or switch
  to a separate `@property` with `functools.cache` on the function, OR make them
  regular `@property` that recompute (acceptable since sha256 is fast). Best pattern:
  use `@property` for `version_hash` and `token_estimate` — frozen dataclass with
  `cached_property` requires `__dict__` which `slots=True` removes.
  Alternatively: drop `slots=True` to allow `cached_property`, or use `__hash__`-based
  LRU caching. **Decide at implementation time; document choice.**
- `DEFAULT_STABILITY` mapping must cover all 10 `BlockKind` values exactly
- `id` regex validation: `^[a-z0-9_./:-]+$` — must not be empty

</specifics>

<deferred>
## Deferred Ideas

- Sorting / compilation logic (P1)
- Linting rules (P2)
- Provider rendering (P3)
- Trace row model `PromptCompilationTrace` (P5)
- Session importers (P6)
- MCP tool handler (P7)
- Python SDK public surface (P8)
- `CompiledPrompt` dataclass (P1)
- `LintFinding` model (P2)
- Provider adapter classes (P3)

</deferred>

---

*Phase: 08-block-model*
*Context gathered: 2026-05-23 via PRD Express Path*
