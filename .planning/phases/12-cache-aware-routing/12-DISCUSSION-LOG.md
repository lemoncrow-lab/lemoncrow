# Phase 12: Cache-Aware Routing - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-28
**Phase:** 12-Cache-Aware Routing
**Areas discussed:** Routing economics, stickiness, telemetry, benchmark proof

---

## Routing Economics

| Option | Description | Selected |
|--------|-------------|----------|
| Follow M2 design | Pure cache-eviction cost vs deterministic quality-gain estimate | ✓ |
| Rebuild routing architecture | Replace router with a new orchestration layer | |
| Learned quality model now | Train or infer quality-gain dynamically in Phase 12 | |

**User's choice:** Autonomous execution using the decisions already in docs.
**Notes:** `docs/plans/context-quality-lift/M2-cache-aware-routing.md` is treated as the locked reference.

---

## Stickiness

| Option | Description | Selected |
|--------|-------------|----------|
| Default window 3 | Preserve route for three follow-up tool calls | ✓ |
| No stickiness | Only use cache-cost comparison | |
| Runtime-only stickiness | Defer all behavior to later runtime wiring | |

**User's choice:** The agent selected the documented default.
**Notes:** Runtime owns reset boundaries at user-visible responses.

---

## Telemetry

| Option | Description | Selected |
|--------|-------------|----------|
| Structured route_decision payload | Emit cache-cost and decision metadata fail-open | ✓ |
| Logs only | Human-readable route rationale without machine-readable fields | |
| No telemetry | Keep routing silent | |

**User's choice:** The agent selected the documented telemetry requirement.
**Notes:** CACHE-04 requires structured fields.

---

## Benchmark Proof

| Option | Description | Selected |
|--------|-------------|----------|
| Deterministic local replay benchmark | Prove >=10% estimated cost reduction without remote models | ✓ |
| Real provider benchmark now | Use external LLM calls during Phase 12 | |
| Unit tests only | Defer CQEVAL-03 proof | |

**User's choice:** The agent selected deterministic local proof first.
**Notes:** Remote/TerminalBench proof is deferred to Phase 15 final gate.

---

## the agent's Discretion

The user delegated implementation details and asked not to be prompted for permission. The agent selected the documented defaults and will proceed to planning/execution autonomously.

## Deferred Ideas

- Phase-linear cache-reuse runner -> Phase 13
- Counterexample loop -> Phase 14
- Scoped pull and final proof gate -> Phase 15
