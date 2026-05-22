# P0 — Block model

> Depends on: nothing.
> Unblocks: P1, P2, P3, P5, P8.

## Goal

Land the data model the rest of the plan builds on: `PromptBlock`,
`Stability`, `BlockKind`, hashing, and a token estimator.

## Files

```
src/atelier/core/capabilities/prompt_compilation/
    __init__.py
    AGENT_README.md
    models.py
    tokens.py
tests/core/capabilities/prompt_compilation/
    test_models.py
    test_tokens.py
```

## Spec

```python
# models.py

class Stability(str, Enum):
    STATIC   = "static"
    SESSION  = "session"
    BRANCH   = "branch"
    TURN     = "turn"
    VOLATILE = "volatile"

class BlockKind(str, Enum):
    TOOL_SCHEMA   = "tool_schema"
    SYSTEM        = "system"
    CODING_POLICY = "coding_policy"
    REPO_SUMMARY  = "repo_summary"
    REASONBLOCK   = "reasonblock"
    FILE_SUMMARY  = "file_summary"
    USER_TASK     = "user_task"
    GIT_DIFF      = "git_diff"
    TOOL_RESULT   = "tool_result"
    SCRATCHPAD    = "scratchpad"

# Each kind has a default stability — the compiler complains if a caller
# overrides it without setting `stability_override_reason`.
DEFAULT_STABILITY: dict[BlockKind, Stability] = {
    BlockKind.TOOL_SCHEMA:   Stability.STATIC,
    BlockKind.SYSTEM:        Stability.STATIC,
    BlockKind.CODING_POLICY: Stability.STATIC,
    BlockKind.REPO_SUMMARY:  Stability.SESSION,
    BlockKind.REASONBLOCK:   Stability.BRANCH,
    BlockKind.FILE_SUMMARY:  Stability.BRANCH,
    BlockKind.USER_TASK:     Stability.TURN,
    BlockKind.GIT_DIFF:      Stability.TURN,
    BlockKind.TOOL_RESULT:   Stability.TURN,
    BlockKind.SCRATCHPAD:    Stability.VOLATILE,
}

@dataclass(frozen=True, slots=True)
class PromptBlock:
    id: str
    kind: BlockKind
    content: str
    stability: Stability
    cacheable: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)
    stability_override_reason: str | None = None

    @cached_property
    def version_hash(self) -> str:
        return sha256(self.content.encode("utf-8")).hexdigest()

    @cached_property
    def token_estimate(self) -> int:
        from .tokens import estimate_tokens
        return estimate_tokens(self.content)
```

`tokens.py` ships `estimate_tokens(text: str, model: str | None = None)`
wrapping `tiktoken` when available with a char/4 fallback. We copy the
pattern from `context_compression/scoring.py` so behaviour matches what
the rest of the codebase already does.

## Invariants enforced at construction

- `content` must be non-empty.
- `id` must match `^[a-z0-9_./:-]+$` (so it round-trips through cache
  keys and trace ids).
- `stability` defaults to `DEFAULT_STABILITY[kind]`; if the caller
  passes a different value, `stability_override_reason` is required.
- `cacheable` defaults to `True`, but is forced to `False` if
  `stability in {TURN, VOLATILE}`.

## Tests

- `test_models.py::test_default_stability_per_kind` — every kind maps.
- `test_models.py::test_override_requires_reason`.
- `test_models.py::test_version_hash_stable_across_processes` — assert
  hash of "hello world" matches a known sha256.
- `test_models.py::test_volatile_forces_uncacheable`.
- `test_tokens.py::test_tiktoken_used_when_available`.
- `test_tokens.py::test_char_fallback_within_15_percent` — uses a fixed
  paragraph; tolerance bounds the fallback drift.

## Acceptance

- `from atelier.core.capabilities.prompt_compilation import PromptBlock,
  Stability, BlockKind` works.
- Tests pass under `uv run pytest tests/core/capabilities/prompt_compilation -q`.
- `make lint && make typecheck` pass.

## Out of scope

- Sorting / compilation (P1).
- Linting (P2).
- Provider rendering (P3).
