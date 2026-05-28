# Plan 01 — Phase-Linear Cache-Reuse Run Mode

> docs/plans/phase-linear-cache-reuse · Plan 01

**Status:** 📋 Proposed

**Owner:** core/capabilities/context_reuse + core/runtime/engine

**Companion docs:** [`00-rationale.md`](./00-rationale.md) (why it works),
[`02-DESIGN-SPEC.md`](./02-DESIGN-SPEC.md) (concrete design).

---

## Goal

Make multi-phase coding runs **materially cheaper and faster at the same model
quality** via two levers:

1. **Warm-prefix reuse** — run the read-heavy Survey and Plan phases as one
   continuous conversation under a single fixed system prompt, so the Plan phase
   reads the Survey history as a provider cache hit instead of re-ingesting it.
2. **Minified reads** — strip non-semantic whitespace from files before they
   enter context (~15–20% fewer tokens on the read path).

Plus a compaction guard so the growing prefix never overruns the cache benefit,
and a benchmark that proves the cost/latency delta.

## Non-goals

- No model change, no fine-tuning. Quality must track the base model.
- Not replacing the cross-run procedural cache (`context_reuse/`); this is a
  complementary provider-side prompt cache for a single run.
- Not removing the per-agent flow; we add a phase-linear mode and select it when
  it wins.

## Context / where it fits

```
gateway/ (cli.py, mcp_server.py, runtime.py)   ← expose mode flag + phase signals (thin)
   ↓
core/runtime/engine.py                         ← drive the single linear conversation
core/capabilities/context_reuse/               ← new PhaseRunner lives here
   ↓
infra/runtime/ (run ledger)                    ← record phase boundaries + cache stats
```

Key invariant (per CLAUDE.md): the capability lives in `core/capabilities/`;
`mcp_server.py` / `cli.py` stay thin dispatchers passing a `mode`/phase signal.

**Reuse what already exists — do not reinvent:**

| Need | Existing module |
|---|---|
| Cache breakpoint planning / diagnostics | `core/capabilities/prefix_cache/` |
| Prefix bloat → summarize & reseed | `core/capabilities/context_compression/`, `optimization/compaction_types.py` |
| Per-turn token/cost accounting | `core/capabilities/pricing.py`, `infra/runtime/` ledger |
| Per-phase model/route choice | `core/capabilities/model_routing/router.py` |

## Design summary

See `02-DESIGN-SPEC.md` for the full schema. In brief: a declarative phase state
machine (Survey → Plan → [review] → Implement); the Plan step continues the
Survey conversation (`continue_from`) for a warm cache; one fixed system prompt
with phase objectives injected as user messages; a read-only tool profile for
Survey/Plan and a read-write profile for Implement; minified reads on the
read-only profile only.

## Implementation steps

1. **Models** — add `Phase`, `PhasePlan`, `PhaseResult`, and cache-stat fields to
   `context_reuse/models.py`.
   **verify:** `uv run pytest tests/core -k phase -q`.
2. **Shared shell prompt** — add the fixed system prompt template + per-phase
   objective templates under the capability's `prompts/`.
   **verify:** unit test asserts the system prompt is identical across phases and
   that each phase objective loads.
3. **PhaseRunner** — single-conversation loop; the Plan phase extends the Survey
   message list; cache breakpoint set at each phase tail; explicit
   `phase_complete` signal for deterministic transitions (fallback: per-phase
   turn/tool budget).
   **verify:** unit test with a fake provider asserts (a) one conversation for
   Survey+Plan, not two; (b) breakpoint at each phase tail; (c) phases run in
   order.
4. **Cache-warmth + stats** — record fresh-input / cache-write / cache-read /
   output per turn into the run ledger; add a TTL warmth guard.
   **verify:** ledger test shows per-phase cache-read fields populated.
5. **Compaction guard** — wire the existing compaction path; trigger on a token
   threshold (summarize prior phases, reseed, reset breakpoint).
   **verify:** test that exceeding the threshold reseeds with carried state.
6. **Source minification** — `minify_source()` on the read path; language-aware
   safety for whitespace-significant languages; record token deltas; applied to
   the read-only profile only.
   **verify:** unit tests assert (a) non-significant whitespace collapses, (b)
   Python/YAML semantics preserved, (c) comprehension preserved on a sample.
7. **Mode selection** — `linear | per_agent | auto` + the `auto` heuristic.
   **verify:** heuristic unit tests for divergent-context and large-prefix cases.
8. **Surfaces** — CLI flag + MCP/runtime param, dispatch only.
   **verify:** `uv run pytest tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py -q`.
9. **Benchmark** — a `linear-vs-per-agent` suite over ~7 scenarios
   (project-from-scratch, OSS bug fix, feature in a large repo, ≥80% coverage,
   refactor, multi-file edit, doc task), reporting cost, wall-time,
   cache-hit ratio, and task success per mode.
   **verify:** `uv run pytest -q -m "not slow"` green; report shows linear mode
   lower cost + time at equal success on context-sharing scenarios.

## Validation

- `make lint && make typecheck && make test`
- `uv run pytest tests/core/test_code_context.py -q` (unaffected)
- Benchmark artifact committed under this plan dir showing the delta.

## Success criteria

- On context-sharing scenarios: **≥30% lower cost and ≥25% lower wall-time** vs.
  the per-agent flow at **equal-or-better task success**, with the measured
  cache-read ratio explaining the delta.
- Minification alone delivers a measurable **15–20% token cut on read context**
  with no comprehension regression in the benchmark.
- `auto` never selects `linear` for a divergent-context scenario where it would
  regress (guarded by the benchmark).

## Risks & open questions

- **Cache TTL races** — biggest risk; mitigated by the warmth guard and by
  measuring real cache-read tokens instead of assuming reuse.
- **Prefix bloat** — mitigated by the compaction threshold; needs tuning.
- **Provider coupling** — cache breakpoints are Anthropic-specific; isolate behind
  the provider adapter so other backends degrade to `per_agent`.
- **Quality drift** — a long single conversation can lose focus; the per-phase
  objective message + tool-grant change re-focus the model at each boundary.
- **Open:** exact compaction threshold and TTL keep-alive policy — settle
  empirically in Step 9.
