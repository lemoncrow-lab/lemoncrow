# Phase-Linear Cache-Reuse Agent — Plan Index

A run mode that makes multi-step coding tasks **cheaper and faster at the same
model quality** by keeping one continuous conversation across the
Survey → Plan → Implement phases so the provider prompt cache stays warm, and by
feeding files into context in a whitespace-minified form. The gain is
architectural — no model change, no quality trade-off.

## Documents

| File | What it is |
|---|---|
| [`00-rationale.md`](./00-rationale.md) | Why it works: prompt-cache economics, the two levers (warm-prefix reuse + minified reads), risks, and where it fits Atelier. |
| [`01-PLAN.md`](./01-PLAN.md) | Implementation plan: PhaseRunner, shared shell prompt, cache-warmth guard, compaction, minification, mode selection, benchmark. |
| [`02-DESIGN-SPEC.md`](./02-DESIGN-SPEC.md) | Concrete design: the phase state-machine schema, the prefix-continuation mechanism, per-phase tool grants, and the exact mapping to Atelier modules. |

## One-paragraph thesis

The usual multi-agent flow runs Survey/Plan/Implement as **separate agents with
different system prompts**, so the provider prompt cache is cold at every phase
boundary and the codebase gets re-ingested at full input price. Instead, run the
read-heavy Survey and Plan phases as **one conversation under a single fixed
system prompt**, announcing each phase via an injected user message. The Plan
phase then reads the entire Survey history as a **cache hit** (~10× cheaper than
fresh input). Stack **whitespace-minified file reads** on top and a typical
feature/bug task costs roughly half as much and finishes meaningfully faster.
Atelier already has the building blocks (`prefix_cache/`, `context_compression/`,
`pricing.py`, `model_routing/`); this plan wires them into a phase-linear run
mode and proves the delta with a benchmark.
