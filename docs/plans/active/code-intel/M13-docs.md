# M13 — Agent-OS playbooks + scorecard metrics

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Blocked by M2, M4, M5. Documentation-only milestone. Stub — flesh out on claim.

## Goal

Make the new tools discoverable and *automatically preferred* by agents.
Update agent-os docs, validation matrix, and scorecard with new patterns and
metrics. Without this milestone, agents will keep reaching for `search +
Read` out of habit and the cost wins evaporate.

## Edits required

### `docs/agent-os/workflow.md`
New section: **Symbol-first navigation.** Mandates:
1. If the symbol name is known → `symbol(name)`, never `search`.
2. For "find code that looks like X" → `pattern(...)`.
3. For "find the thing called X and everything that uses it" → `symbol` then `usages`.
4. For refactors → `edit(op="symbol")` or `pattern(..., rewrite=...)`, not raw `Edit`.

### `docs/agent-os/taste-invariants.md`
New invariants:
- *"If the caller already knows the symbol name, do not run a text search."*
- *"Default to outline-first responses. Expand only on intent."*
- *"Never edit at line numbers when the target is a named symbol."*

### `docs/agent-os/validation-matrix.md`
Add a row per new tool with its validation gates (carry the rows from each Mn file).

### `docs/architecture/README.md`
Lift the stack diagram from `index.md` into the canonical architecture doc.

### `docs/quality/scorecard.md`
New metrics:
- *% of code-intel tool calls hitting cache* — target ≥ 40% steady state.
- *% of navigation tasks using `symbol()` vs `search()`* — target ≥ 70% within two weeks of M2 landing.
- *Median tokens per navigation task* — target ≤ 25% of pre-M2 baseline.
- *Median tokens per refactor task* — target ≤ 30% of pre-M5 baseline.
- *Bootstrap cost per workspace* — record, no target.

### `docs/decisions/001-symbol-first-mcp.md`
Move to `accepted` once M2 + M5 ship, link to the bench results.

## To flesh out on claim

- Concrete copy for each section.
- Worked examples in `workflow.md` showing a before/after.
- Scorecard data plumbing — where do the metrics actually come from (telemetry export)?

## Exit criteria

- All listed docs updated.
- Scorecard metrics live in the Insights tab.
- New ADR status `accepted`.
- Agents demonstrably reach for `symbol` first on internal test runs.
