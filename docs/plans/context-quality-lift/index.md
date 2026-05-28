# Context Quality Lift — close the agent-quality gap with better context

> Status: **Active** — created 2026-05-28.
> Owner: unassigned.
> Origin: Augment Code research memo (May 2026). See [`grounding.md`](grounding.md) for the gap analysis.

## North star

**Make the same underlying model (Claude / Codex / Gemini) measurably better at coding tasks by feeding it better-scoped, history-aware context — and by refusing to break the KV-cache for marginal routing wins.**

The research finding that drives this plan: on SWE-bench Pro, **the same Claude Opus 4.5** solves 15–17 more problems when run through Augment's infrastructure than through Claude Code or Cursor. The model is identical; the context pipeline differs. This plan ports the load-bearing pieces of that pipeline into Atelier, reusing existing capabilities wherever possible.

## Scope

4 milestones, each ≤1 week, each independently shippable, each measured against a baseline.

| ID | File | What it ships | Effort |
|----|------|---------------|--------|
| M1 | [`M1-context-lineage.md`](M1-context-lineage.md) | LLM-summarised commit history embedded alongside code chunks. Agent answers "why did we change this?" and "is there prior art for this pattern?" without re-reading git log. | 2–3 days |
| M2 | [`M2-cache-aware-routing.md`](M2-cache-aware-routing.md) | Wire `prefix_cache/planner.py` into `model_routing/router.py`. Router refuses to switch models when the KV-cache delta would exceed the quality delta. Sticky routes within a tool-call chain. | 1–2 days |
| M3 | [`M3-counterexample-loop.md`](M3-counterexample-loop.md) | Layered verification: deterministic checks (lint/type/test) → structured counterexamples → agent retry. Re-uses `proof_gate/` and `failure_analysis/`. | 3–4 days |
| M4 | [`M4-scoped-pull-context.md`](M4-scoped-pull-context.md) | Explicit "given this subtask, return minimal scoped context" API over existing `context_reuse` + `code_context`. Pull model per subtask, not per session. | 2–3 days |

Deliberately **out of scope** (see [`grounding.md`](grounding.md) §"Not in this plan"):

- Quantized ANN vector search (matters at >10M LOC; not Atelier's bottleneck today).
- Custom code-specific embedding models with hard-negative mining (months of R&D; marginal gain vs. better retrieval composition).
- Full DAG-based Coordinator-Implementor-Verifier decomposition (large effort; value is parallel implementors in worktrees — defer until SWE-bench numbers are the goal).
- Cloud-hosted multi-tenant index (Augment's Bigtable stack). Atelier is local-first.

## Why these four, in this order

1. **M1 first** because it's the single highest-leverage gap. Atelier already walks git history (`infra/code_intel/git_history/walker.py`); it just doesn't summarise or embed commits. Adding a summariser + embed-into-existing-store closes the gap with the least new code.
2. **M2 second** because the pieces both exist — `prefix_cache/planner.py` computes prefix hashes and invalidation reasons; `model_routing/router.py` decides tiers — they just don't talk. Wiring them together is days of work for a measurable cost/quality win.
3. **M3 third** because once context is rich (M1) and routing is cache-stable (M2), the next bottleneck is the agent acting on faulty intermediate output. Structured counterexample feedback is the documented Augment technique; `proof_gate/` and `failure_analysis/` are already partway there.
4. **M4 last** because scoped pull-context only pays off once the upstream context engine is rich enough that *unscoped* retrieval becomes noisy. M1 makes it rich; M4 then keeps it tight.

## What we deliberately reuse (don't rebuild)

| Atelier asset | Milestone | Why it fits |
|---|---|---|
| `infra/code_intel/git_history/walker.py` | M1 | Already enumerates commits; just needs a summariser hook |
| `core/capabilities/code_context/intel_store.py` | M1 | SQLite schema accepts new chunk types; embedding ranker already exists |
| `core/capabilities/prefix_cache/planner.py` | M2 | Computes `prefix_hash`, `invalidated_reason`, token splits |
| `core/capabilities/model_routing/router.py` | M2 | 5-tier route taxonomy; just needs cache-cost signal |
| `core/capabilities/proof_gate/capability.py` | M3 | Cost-quality gating exists; needs per-step counterexample emit |
| `core/capabilities/failure_analysis/` | M3 | Failure parsing primitives |
| `core/capabilities/context_reuse/capability.py` | M4 | BM25 + dead-ends + ranking already done |
| `core/capabilities/code_context/engine.py` | M4 | Token-budget packer + outline-first already enforced |

## Validation gates (cross-milestone)

Each milestone must land:

- A **before/after benchmark** under `tests/benchmarks/context_quality/` showing the metric improvement claimed in its milestone file.
- A **trace recorded** via `mcp__atelier__trace` referencing the milestone ID.
- A new row in `docs/agent-os/validation-matrix.md`.
- Unit tests covering the new code path.
- **No regression** in existing `tests/core/test_code_context.py`, `tests/core/test_model_routing.py`, or `tests/core/test_proof_gate.py`.

## Success metric (whole plan)

A single internal eval, run before M1 and after M4:

- Corpus: 30 multi-file edit tasks drawn from this repo's own commit history (real bugs/features we already solved).
- Agent: Claude Sonnet 4.6 driven through Atelier, with each milestone progressively enabled.
- Metric: % of tasks where the agent produces a patch that passes the original PR's test suite without human edit.
- Target: +15 percentage points absolute lift from M0 baseline to M4 enabled. (Augment claims ~+5 SWE-bench-Pro points end-to-end; +15 against our own repo is a higher-signal proxy because we control the ground truth.)

If M1 alone does not produce ≥+5 points, stop and reassess the plan before starting M2.

## Dependency graph

```
M1 (context lineage)
 └─► M4 (scoped pull context)   ← M4 wants M1's commit chunks in the candidate set

M2 (cache-aware routing)        ← independent
M3 (counterexample loop)        ← independent; benefits from M2 (cheaper retries)
```

Recommended build order: **M1 → M2 → M3 → M4**.
M2 can run in parallel with M1 if a second contributor is available.

## Open questions

- Which model summarises commits in M1? Local SLM (free, slow), Haiku/Flash (cheap, fast), or batch nightly with frontier? Decide on M1 claim.
- Where do counterexamples land in the prompt — system, user, or tool-result channel? Affects prefix-cache stability (M2). Resolve in M3 design.
- Do we ship M1's commit chunks as a new `code` op or fold into existing `code op="search"` results? Lean toward fold-in (caller doesn't need to know).
