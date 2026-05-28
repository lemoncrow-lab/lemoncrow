# M3 — Layered verification with structured counterexamples

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).

## Goal

When an agent's intermediate output fails a deterministic check (lint, type, test), feed the failure back as a **structured counterexample**, not a binary pass/fail. The agent retries with the failing signal in hand, capped at N attempts.

Net effect: bugs surface inside the loop instead of after the user merges. Retry budget becomes productive instead of looping.

## Augment reference

[CIV pattern guide](https://www.augmentcode.com/guides/coordinator-implementor-verifier):

> Layered pipelines combining (in order): deterministic checks (linters, type checkers, test runners), structured LLM semantic review, then feedback formatted as counterexamples or compiler errors — not binary pass/fail.

> Typically capped at 3 attempts per subtask, 5-iteration replanning limit.

## Background

Atelier already has:

- `core/capabilities/proof_gate/capability.py` — cost-quality gating, but it runs at **release-gate time**, not inside the agent loop.
- `core/capabilities/failure_analysis/capability.py` — primitives for parsing failures.
- `core/capabilities/loop_detection/rescue.py` — detects when the agent is spinning. (Atelier-ahead of Augment here.)

What's missing: a per-step verifier that runs **between agent actions** and emits a structured counterexample object the next prompt can ingest.

## Module layout

```
src/atelier/core/capabilities/verification/      (new)
  __init__.py
  capability.py         — VerifierCapability orchestrator
  checks/
    __init__.py
    lint.py             — invoke ruff / eslint / etc; parse output
    typecheck.py        — invoke mypy / tsc / etc
    tests.py            — invoke pytest -q -k <scope> ; parse output
    semantic_review.py  — small-LLM "does this match the stated intent?" review
  counterexample.py     — structured Counterexample model + prompt formatter
  budget.py             — retry budget tracker (default 3 per subtask)
src/atelier/core/capabilities/proof_gate/
  capability.py         (extend) — accept verification trace as evidence
```

Why a new top-level capability rather than extending `proof_gate/`: `proof_gate` is a release-time concept ("can this ship?"). Per-step verification is a runtime-loop concept. Conflating them blurs both.

## Counterexample contract

```python
@dataclass
class Counterexample:
    check: Literal["lint", "typecheck", "tests", "semantic"]
    severity: Literal["error", "warn"]
    file_path: str | None
    line: int | None
    diagnostic: str           # raw tool output, trimmed
    expected: str | None      # what should have been true
    actual: str | None        # what is true
    repro_command: str | None # exact command to re-run this check

    def to_prompt_block(self) -> str:
        # Renders as a structured block the agent can ingest:
        #   <counterexample check="typecheck" severity="error" file="foo.py" line=42>
        #     expected: x is int
        #     actual:   x is str | None
        #     repro:    uv run mypy src/foo.py
        #     diagnostic: foo.py:42: error: Incompatible types ...
        #   </counterexample>
        ...
```

## Loop integration

The verifier runs **after the agent declares an edit complete**, before the next user turn. Pseudocode:

```python
for attempt in range(budget.max_attempts):  # default 3
    edit_result = agent.act(prompt)
    if not edit_result.touched_files:
        break
    failures = verifier.run(
        scope=edit_result.touched_files,
        checks=["lint", "typecheck", "tests"],
    )
    if not failures:
        break
    counterexamples = [Counterexample.from_failure(f) for f in failures]
    prompt = prompt.with_counterexamples(counterexamples)
    budget.consume()
else:
    rescue.invoke(reason="verification_budget_exhausted", failures=failures)
```

`rescue.invoke` already exists in `loop_detection/rescue.py`; M3 wires it as the budget-exhaustion sink.

## Check scoping (don't over-verify)

- **lint**: only on files the agent touched this attempt.
- **typecheck**: only on the package containing touched files.
- **tests**: only tests whose paths or names match touched files; full suite is too slow inside a loop.
- **semantic**: cheap-LLM "did this edit match the stated subtask intent?" — runs once per attempt, not per file.

## Prompt-channel placement (cache-stability constraint)

Counterexamples land in the **tool-result channel**, not the system prompt. Rationale (cross-referenced with M2): system prompts are STATIC stability; mutating them per retry would evict the prefix cache. Tool results are TURN stability; they naturally don't anchor cache.

This decision must be enforced in `prompt_compilation/` — add a sanity check that Counterexample blocks are never emitted with Stability ≥ BRANCH.

## Validation

Tests under `tests/core/test_verification/`:

- `test_lint_failure_becomes_counterexample.py` — synthetic ruff failure → structured Counterexample with correct file/line/diagnostic.
- `test_typecheck_scoping.py` — only relevant package is mypy'd.
- `test_budget_exhaustion_invokes_rescue.py` — 3 failed attempts → rescue called.
- `test_counterexample_never_static.py` — prompt compiler rejects Counterexample blocks with Stability ≥ BRANCH.

Benchmark under `tests/benchmarks/context_quality/M3_verification.py`:

- 20 synthetic edits seeded with deliberate type errors.
- Metric: % of edits the agent self-corrects within budget.
- Target: ≥60% self-correction rate; baseline (no counterexamples) expected ≤15%.

## Exit criteria

- `verification` capability lands with the four checks above.
- Counterexamples render via `prompt_compilation` at correct stability level.
- Loop integration wired in the agent runtime.
- Rescue called on budget exhaustion (not silent failure).
- Benchmark target hit (≥60%).
- No regression in `tests/core/test_proof_gate.py`.

## Open questions

- Do we run `tests` inside the loop on slow projects (e.g. Atelier itself takes ~30s)? Default: yes for any test whose path matches touched files; configurable timeout that demotes to lint+typecheck only.
- Does the semantic-review check use the same model as the main agent, or a cheaper one? Lean toward cheap — its job is rough correctness, not deep reasoning.
- How do we surface the retry attempt count to the user? Probably in the trace; not in the visible response unless rescue fires.
