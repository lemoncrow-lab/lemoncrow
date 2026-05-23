# Phase 1: Output Policy Foundation - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-23
**Phase:** 1-output-policy-foundation
**Areas discussed:** Default cap values per mode/operation

---

## Default cap values per mode/operation

| Option | Description | Selected |
|--------|-------------|----------|
| Use proposed baseline caps | search 1800, relation 2200, context 6500, node outline 3000, node code 2500 | ✓ |
| Start with relaxed context caps | keep search/relation baseline and use a looser first-pass context target | |
| Conservative rollout | enforce hard caps first and finalize cap values in Phase 2 | |

**User's choice:** Use proposed baseline caps.
**Notes:** Baseline caps are accepted as milestone-aligned defaults for compact behavior.

## budget_tokens precedence

| Option | Description | Selected |
|--------|-------------|----------|
| Override defaults with safety max | `budget_tokens` can tune output but cannot exceed operation safety cap | ✓ |
| Full budget control | `budget_tokens` fully controls response size with no extra safety guard | |
| Ignore budget_tokens in compact mode | always use fixed compact caps regardless of caller budget | |

**User's choice:** `budget_tokens` overrides defaults but still respects operation safety max.
**Notes:** Keep explicit caller control, but enforce hard safety boundaries globally.

---

## the agent's Discretion

- Final helper API names and module placement for policy/truncation utilities.
- Exact truncation boundary behavior, as long as hard caps are guaranteed.

## Deferred Ideas

None.
