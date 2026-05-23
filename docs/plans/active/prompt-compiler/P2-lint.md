# P2 — Cache-safety linter

> Depends on: P1.
> Unblocks: P4, P6, P7.

## Goal

Catch the cache breakers that destroy prefix caching in the wild — and
give the user actionable diagnostics, not just pass/fail.

## Files

```
src/atelier/core/capabilities/prompt_compilation/
    linter.py
    lint_rules/
        __init__.py
        ordering.py
        tools.py
        content.py
        size.py
tests/core/capabilities/prompt_compilation/
    test_linter.py
```

## Spec

```python
class Severity(str, Enum):
    ERROR   = "error"
    WARN    = "warn"
    INFO    = "info"

@dataclass(frozen=True)
class LintFinding:
    rule_id: str
    severity: Severity
    block_id: str | None
    message: str
    fix_hint: str | None = None

@dataclass(frozen=True)
class LintReport:
    findings: tuple[LintFinding, ...]
    cache_score: int          # 0–100; 100 = no errors, no warnings
    stable_prefix_tokens: int
    volatile_tokens_before_prefix_end: int
    estimated_openai_cache_eligible: bool   # ≥1024 tokens before tail
    anthropic_breakpoint_target_block_id: str | None

def lint(compiled: CompiledPrompt, *, previous_compile: CompiledPrompt | None = None) -> LintReport: ...
```

`previous_compile` is optional. When passed, the linter can compare
tool-schema order across turns and surface "tool list order changed
since last call".

## Rule catalogue (initial set)

### Ordering rules (`lint_rules/ordering.py`)

| Rule id | Severity | Detects |
|---|---|---|
| `ordering.volatile-before-stable` | ERROR | Any `VOLATILE` or `TURN` block appears at an index less than the last stable block's index. |
| `ordering.session-before-static` | WARN | A session block precedes a static block — violates the canonical order even though the prefix may still cache. |
| `ordering.unstable-stability-override` | WARN | A block has `stability_override_reason` set; surfaces every override for review. |

### Tool-schema rules (`lint_rules/tools.py`)

| Rule id | Severity | Detects |
|---|---|---|
| `tools.reordered-since-previous` | ERROR | Compared to `previous_compile`, the sequence of `tool_schema` ids changed. |
| `tools.added-mid-session` | ERROR | A tool id present in this compile is missing from `previous_compile` (and vice versa). |
| `tools.duplicate-id` | ERROR | Two `tool_schema` blocks share an id. |

### Content rules (`lint_rules/content.py`)

| Rule id | Severity | Detects |
|---|---|---|
| `content.timestamp-in-prefix` | ERROR | Regex match for ISO-8601 timestamps inside any `STATIC` / `SESSION` block. |
| `content.uuid-in-prefix` | ERROR | UUIDv4 regex match in the stable prefix. |
| `content.request-id-in-prefix` | ERROR | Regex `request[_-]?id\s*[:=]\s*\S+` in the stable prefix. |
| `content.raw-log-in-turn` | WARN | A `tool_result` block over the configured size threshold (default 4,000 tokens) and not pre-compressed. |
| `content.scratchpad-in-prefix` | ERROR | A `SCRATCHPAD` block ended up in the stable prefix range (would be a P0/P1 bug). |

### Size rules (`lint_rules/size.py`)

| Rule id | Severity | Detects |
|---|---|---|
| `size.prefix-below-openai-floor` | WARN | Stable prefix < 1,024 tokens — OpenAI caching will not activate. |
| `size.prefix-tiny-for-anthropic` | INFO | Stable prefix < 4,096 tokens — Anthropic cache write cost may dominate. |
| `size.tail-exceeds-budget` | WARN | Dynamic tail > 25% of total tokens; large tails are usually a sign of unsummarized tool output. |

## Cache score

```
score = 100
for finding in findings:
    if finding.severity == Severity.ERROR: score -= 15
    if finding.severity == Severity.WARN:  score -= 5
    if finding.severity == Severity.INFO:  score -= 1
score = max(score, 0)
```

The exact weights aren't sacred — the point is to give a single number
the dashboard can sort by. P5 records this as `cache_lint_score`.

## Plug-in API

`lint_rules` is a registry: each module exports a `RULES` list of
`LintRule` instances. Adding a rule = drop a file in
`lint_rules/<area>.py` and register the new rule in `__init__.py`.

```python
class LintRule(Protocol):
    rule_id: str
    severity: Severity
    def check(self, compiled: CompiledPrompt, previous: CompiledPrompt | None) -> Iterable[LintFinding]: ...
```

## Tests

- `test_linter.py::test_volatile_before_stable_errors`.
- `test_linter.py::test_timestamp_in_static_block_errors`.
- `test_linter.py::test_tool_reorder_errors_with_previous`.
- `test_linter.py::test_no_previous_means_no_tool_diff_errors`.
- `test_linter.py::test_prefix_below_1024_warns`.
- `test_linter.py::test_cache_score_monotonic` — adding rules can only
  decrease score.
- `test_linter.py::test_clean_input_scores_100`.

## Acceptance

- `lint(compiled)` returns a `LintReport` in <10 ms for 100 blocks.
- `make lint && make typecheck` pass.
- Rule docs auto-generated into `docs/agent-os/prompt-compiler-rules.md`
  (deferred to P4, but P2 must expose the rule metadata for that doc to
  generate).

## Out of scope

- Renderer / provider-specific checks (P3 adds a small adapter-level
  lint pass for things like "Anthropic breakpoint target missing").
- Auto-fix. Linter reports; it doesn't rewrite.
