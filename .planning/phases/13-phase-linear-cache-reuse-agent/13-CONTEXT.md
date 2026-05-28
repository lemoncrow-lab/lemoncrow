# Phase 13: Phase-Linear Cache-Reuse Agent - Context

**Gathered:** 2026-05-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 13 delivers a phase-linear run mode for coding workflows: Survey and Plan run as one cache-warm read-only conversation under a fixed system prompt, Implement starts as a separate lean writer step, read-only file context may be safely minified, and runtime mode selection exposes `linear | per_agent | auto` with a local benchmark proving cost/time wins without task-success regression.

</domain>

<decisions>
## Implementation Decisions

### Locked Plan Shape
- **D-01:** Treat `docs/plans/phase-linear-cache-reuse/` as the locked design input. Downstream agents must follow the rationale, plan, and design spec before inventing alternatives.
- **D-02:** Minimal viable version is exactly the design spec's three load-bearing pieces: Survey→Plan continuation, phase behavior via injected user messages over one fixed system prompt, and minified reads during Survey/Plan.
- **D-03:** Implement and test in `core/capabilities/context_reuse/` and `core/runtime/engine.py`; gateway surfaces stay thin dispatchers only.

### Conversation and Cache Semantics
- **D-04:** Survey and Plan share one message list. Plan must use `continue_from="survey"` and must not re-open a fresh conversation for already-read Survey context.
- **D-05:** Implement does not continue Survey/Plan history by default. It receives the accepted plan and starts lean with writer tools and exact-byte reads.
- **D-06:** Keep one fixed system prompt across phases. Phase-specific behavior lives in small injected user objective messages, not per-phase system prompts, so the cacheable prefix remains stable.
- **D-07:** Set/record cache breakpoints at phase tails and record actual cache-read/cache-write/fresh-input/output token counts. Do not assume cache reuse from structure alone.

### Tool Profiles and Minified Reads
- **D-08:** Reader profile is read/search/code-intel only; writer profile adds edit/write/delete capabilities. The read-only grant structurally enforces plan-before-mutation.
- **D-09:** `minify_source()` is a read-context optimization only. Writer paths must read exact bytes.
- **D-10:** Preserve Python/YAML semantics by using conservative line trimming for whitespace-significant formats; more aggressive whitespace collapse is allowed only for languages where it is safe and tested.
- **D-11:** Record original vs minified token counts per read/phase so the benchmark can attribute savings to minification separately from cache reuse.

### Mode Selection and Fallbacks
- **D-12:** Add explicit modes `linear`, `per_agent`, and `auto`. `auto` chooses linear only for context-sharing scenarios with projected prefix under threshold.
- **D-13:** Fall back to `per_agent` for divergent sub-contexts, oversized prefixes, or providers where cache-breakpoint semantics are unavailable or unmeasurable.
- **D-14:** Cache TTL and prefix bloat are first-class guards. If a handoff is too slow or prefix too large, either record a cold-read/fallback or compact/reseed with explicit evidence.

### Benchmark and Proof
- **D-15:** The Phase 13 benchmark must cover at least seven representative scenarios and report cost, wall time, cache-hit/cache-read ratio, minification delta, and task success.
- **D-16:** Success target is ≥30% lower cost and ≥25% lower wall-time at equal-or-better task success on context-sharing scenarios.
- **D-17:** Benchmark artifacts belong under `docs/plans/phase-linear-cache-reuse/` or the phase directory and must separate cache-reuse savings from minification savings.

### Existing Dirty Work
- **D-18:** Current uncommitted changes in `src/atelier/core/capabilities/context_reuse/capability.py`, `src/atelier/core/runtime/engine.py`, and `tests/core/test_capabilities_production.py` are treated as user/ongoing work. Executors must inspect and preserve them; do not overwrite or "fix" them just to make tests pass.

### the agent's Discretion
The planner may choose the exact dataclass field names, runner API shape, and benchmark fixture mechanics as long as they satisfy the locked docs, requirements, and validation targets above.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Locked Phase-Linear Design
- `docs/plans/phase-linear-cache-reuse/index.md` — Plan index and one-paragraph thesis for the phase-linear run mode.
- `docs/plans/phase-linear-cache-reuse/00-rationale.md` — Prompt-cache economics, minified-read rationale, risks, and where the feature fits Atelier.
- `docs/plans/phase-linear-cache-reuse/01-PLAN.md` — Required implementation steps, validation, success criteria, and non-goals.
- `docs/plans/phase-linear-cache-reuse/02-DESIGN-SPEC.md` — Concrete state-machine schema, `continue_from` semantics, phase objectives, tool profiles, telemetry, and mode selection.

### Project and Requirement State
- `.planning/REQUIREMENTS.md` — LINEAR-01 through LINEAR-05 and TBEVAL-01 are the Phase 13 acceptance requirements.
- `.planning/ROADMAP.md` — Phase 13 scope, key modules, dependencies, and success criteria.
- `.planning/phases/12-cache-aware-routing/12-01-SUMMARY.md` — Phase 12 outputs available to Phase 13: cache-aware router telemetry and pricing helpers.

### Existing Code Touchpoints
- `src/atelier/core/capabilities/context_reuse/models.py` — Current context reuse dataclasses; extend here for phase state-machine models.
- `src/atelier/core/capabilities/context_reuse/capability.py` — Existing retrieval capability and current dirty user changes; inspect before editing.
- `src/atelier/core/capabilities/context_compression/capability.py` — Existing compression primitives; reuse where possible for compaction/reseed.
- `src/atelier/core/capabilities/context_compression/models.py` — Existing compression result models; extend or reuse for minification telemetry as appropriate.
- `src/atelier/core/capabilities/prefix_cache/planner.py` — Prefix plan and cache diagnostics; use for stable prefix/cache-breakpoint reasoning.
- `src/atelier/core/capabilities/model_routing/router.py` — Phase 12 cache-aware recommendations and route-decision fields.
- `src/atelier/infra/runtime/run_ledger.py` — Existing `record_call()` token/cost fields; extend carefully for per-phase cache stats if needed.
- `src/atelier/core/runtime/engine.py` — Runtime dispatch integration point and current dirty user changes; inspect before editing.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `PrefixCachePlan` and `PrefixCachePlanner` already expose prefix hash, prefix tokens, dynamic tokens, total tokens, and invalidation reason for cache diagnostics.
- `RunLedger.record_call()` already records input/output/cache-read token counts, cost, prompt/response, stable prefix hash, and invalidation reason.
- `ModelRouter.recommend()` now emits cache-aware decision data and can provide telemetry that `auto` mode can consume.
- `ContextCompressionCapability` already has token-budget compression and sleeptime summarization paths that can be reused for prefix-bloat compaction/reseed.

### Established Patterns
- Core capability logic belongs under `src/atelier/core/capabilities/`; gateway/CLI/MCP code must stay thin.
- Public data contracts should be typed dataclasses/Pydantic models with explicit `to_dict()` helpers when serialized.
- Fail-open telemetry is allowed for observability sinks, but benchmark/proof paths must not silently assume cache hits.
- All Python commands must use `uv run`; broad typecheck/test failures may be pre-existing and should not trigger unrelated code changes.

### Integration Points
- Add phase state-machine models under `context_reuse/models.py`.
- Add the new `PhaseRunner` under `context_reuse/`, not in gateway or MCP server code.
- Add fixed prompts under `context_reuse/prompts/`.
- Add or extend a context-compression read-path helper for `minify_source()`.
- Extend `AtelierRuntimeCore` with mode dispatch while preserving existing `get_context()` behavior and dirty user changes.

</code_context>

<specifics>
## Specific Ideas

Use the design spec's Survey → Plan → Implement state machine as the canonical shape. Keep Survey/Plan read-only, make Plan continue Survey history, and make Implement a separate writer step. Treat the benchmark as part of the feature, not a follow-up.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 13-Phase-Linear Cache-Reuse Agent*
*Context gathered: 2026-05-28*
