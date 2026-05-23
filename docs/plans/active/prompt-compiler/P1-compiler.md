# P1 — Compiler core

> Depends on: P0.
> Unblocks: P2, P3, P4, P5, P7, P8.

## Goal

Produce a deterministic, cache-safe `CompiledPrompt` from a list of
`PromptBlock`s. This is the only place in the codebase that decides the
order of blocks.

## Files

```
src/atelier/core/capabilities/prompt_compilation/
    compiler.py
    capability.py
tests/core/capabilities/prompt_compilation/
    test_compiler.py
```

## Spec

```python
# compiler.py

STABILITY_ORDER = {
    Stability.STATIC:   0,
    Stability.SESSION:  1,
    Stability.BRANCH:   2,
    Stability.TURN:     3,
    Stability.VOLATILE: 4,
}

KIND_ORDER = {
    # Inside the static tier, this is the canonical order.
    BlockKind.TOOL_SCHEMA:   0,
    BlockKind.SYSTEM:        1,
    BlockKind.CODING_POLICY: 2,
    # Session
    BlockKind.REPO_SUMMARY:  10,
    # Branch
    BlockKind.REASONBLOCK:   20,
    BlockKind.FILE_SUMMARY:  21,
    # Turn
    BlockKind.USER_TASK:     30,
    BlockKind.GIT_DIFF:      31,
    BlockKind.TOOL_RESULT:   32,
    # Volatile
    BlockKind.SCRATCHPAD:    40,
}

@dataclass(frozen=True)
class CompiledPrompt:
    blocks: tuple[PromptBlock, ...]
    prefix_end_index: int       # inclusive index of the last stable block
    stable_prefix_hash: str
    stable_prefix_tokens: int
    dynamic_tail_tokens: int

def compile_prompt(
    blocks: Iterable[PromptBlock],
    *,
    tail_budget_tokens: int | None = None,
) -> CompiledPrompt: ...
```

### Sort key

```
(STABILITY_ORDER[block.stability], KIND_ORDER[block.kind], block.id)
```

Ties resolve by `id` so two file-summary blocks for the same task end up
in a stable order even when inputs are reordered upstream.

### Prefix boundary

The "stable prefix" is the contiguous run of blocks with
`stability in {STATIC, SESSION, BRANCH}` starting at index 0.

`prefix_end_index` = index of the last such block. If there are no
stable blocks, `prefix_end_index = -1`.

### Prefix hash

```
stable_prefix_hash = sha256(
    b"\n--BLOCK--\n".join(
        f"{b.kind}:{b.id}:{b.version_hash}".encode("utf-8")
        for b in blocks[: prefix_end_index + 1]
    )
).hexdigest()
```

This is the value we record on traces. Two compiles whose hashes match
will hit provider-side cache; two that differ will not.

### Tail budget packing

If `tail_budget_tokens` is set, the compiler:

1. Counts tokens of all turn + volatile blocks.
2. If under budget, passes them through unchanged.
3. If over budget, hands the turn + volatile slice to
   `PromptBudgetOptimizer.solve(...)` from `budget_optimizer`. The
   stable prefix is never touched.

The optimizer needs per-block `utility`. We use the following
heuristic and document it as the default policy:

| Kind | Utility |
|---|---|
| USER_TASK | 1.0 (always kept; if it would be dropped, raise) |
| GIT_DIFF | 0.9 |
| TOOL_RESULT (errors) | 0.8 |
| TOOL_RESULT (success) | 0.5 |
| SCRATCHPAD | 0.3 |

Callers can override by setting `metadata["utility"]` on the block.

## Determinism guarantees (and tests for them)

- `compile_prompt(shuffle(blocks))` returns the same `CompiledPrompt`
  regardless of input order. Test with `random.Random(seed).shuffle`.
- `stable_prefix_hash` is stable across processes (sha256 has no
  randomness). Test by asserting the hash of a fixed input matches a
  hard-coded sha256.
- `compile_prompt(blocks)` is referentially transparent: the same input
  produces the same output, every time.

## Capability wrapper

`capability.py` exposes:

```python
class PromptCompilerCapability:
    def __init__(self, telemetry: TelemetrySubstrate | None = None) -> None: ...
    def compile(self, blocks: Sequence[PromptBlock], *, tail_budget_tokens: int | None = None) -> CompiledPrompt: ...
```

Stamps one trace per `compile()` (P5 fleshes out the fields). The
capability is registered in `CapabilityRegistry` so `engine.py` can
look it up by name.

## Tests

- `test_compiler.py::test_stable_blocks_sort_before_volatile`.
- `test_compiler.py::test_shuffle_invariant` (parametrize over 50 seeds).
- `test_compiler.py::test_prefix_hash_matches_golden_sha256`.
- `test_compiler.py::test_prefix_end_index_when_no_stable_blocks`.
- `test_compiler.py::test_tail_budget_drops_low_utility_first`.
- `test_compiler.py::test_user_task_never_dropped` — even when budget
  is impossibly small, the user task survives; the optimizer call must
  raise `BudgetTooSmall` instead of silently dropping it.

## Acceptance

- `compile_prompt([...])` returns a `CompiledPrompt` with the documented
  invariants in <5 ms for 100 blocks.
- Tests pass; `mypy --strict src/atelier/core/capabilities/prompt_compilation` is clean.

## Out of scope

- Linting (P2) — the compiler trusts its inputs; the linter is a
  separate pass callers run when they want diagnostics.
- Provider-specific rendering (P3).
- MCP wiring (P7).
