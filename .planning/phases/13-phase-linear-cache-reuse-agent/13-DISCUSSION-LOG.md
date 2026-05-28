# Phase 13: Phase-Linear Cache-Reuse Agent - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-28
**Phase:** 13-Phase-Linear Cache-Reuse Agent
**Areas discussed:** Locked design ingestion, cache semantics, minification/tool profiles, mode selection, benchmark proof, dirty work preservation

---

## Locked Design Ingestion

| Option | Description | Selected |
|--------|-------------|----------|
| Follow `docs/plans/phase-linear-cache-reuse/` exactly | Treat the existing rationale, plan, and design spec as locked input. | ✓ |
| Re-open design questions | Ask the user to re-decide design details already captured in docs. | |

**User's choice:** User previously instructed the agent to use the phase-linear cache-reuse docs as locked input and continue autonomously.
**Notes:** No additional user questions were needed; this phase is strongly specified.

---

## Cache Semantics

| Option | Description | Selected |
|--------|-------------|----------|
| Survey→Plan shared conversation | Plan continues Survey with one fixed system prompt and phase objective user messages. | ✓ |
| Separate agents for every phase | Existing flow, but loses provider prefix cache warmth. | |

**User's choice:** Locked by `02-DESIGN-SPEC.md`.
**Notes:** Implement remains separate and lean as a writer step.

---

## Minification and Tool Profiles

| Option | Description | Selected |
|--------|-------------|----------|
| Read-only minified context | Apply safe minification only for Survey/Plan reader profile; writer uses exact bytes. | ✓ |
| Universal minification | Minify all reads including writer reads. | |

**User's choice:** Locked by `01-PLAN.md` and `02-DESIGN-SPEC.md`.
**Notes:** Python/YAML need conservative preservation behavior.

---

## Mode Selection

| Option | Description | Selected |
|--------|-------------|----------|
| `linear | per_agent | auto` | Explicit modes with `auto` choosing linear only when context-sharing and prefix size make it beneficial. | ✓ |
| Always linear | Simpler but unsafe for divergent or oversized contexts. | |

**User's choice:** Locked by roadmap and design docs.
**Notes:** Fallbacks must be evidence-based and not silently assume cache reuse.

---

## Benchmark Proof

| Option | Description | Selected |
|--------|-------------|----------|
| Seven-scenario local benchmark | Report cost, wall time, cache hit/read ratio, minification delta, and task success. | ✓ |
| Unit tests only | Proves mechanics but not the product claim. | |

**User's choice:** Locked by LINEAR-05 and TBEVAL-01.
**Notes:** The benchmark target is ≥30% cost reduction and ≥25% wall-time reduction at equal-or-better success.

---

## Dirty Work Preservation

| Option | Description | Selected |
|--------|-------------|----------|
| Preserve and inspect dirty files | Treat uncommitted context_reuse/runtime/test changes as user/ongoing work. | ✓ |
| Overwrite dirty files from the plan | Risk losing intentional user changes. | |

**User's choice:** Follows user memory: do not modify code just to make tests pass; ask before changing conflicting intentional changes.
**Notes:** Executors must read diffs before editing `context_reuse/capability.py`, `runtime/engine.py`, or `tests/core/test_capabilities_production.py`.

---

## the agent's Discretion

- The agent may choose exact dataclass and helper names where the design docs do not specify them.
- The planner may split implementation into multiple plans if that better preserves atomic validation and dirty-work safety.

## Deferred Ideas

None.
