# P8 — Python SDK

> Depends on: P1, P3, P5 (the SDK's contract = compile + render + attach_usage).
> Unblocks: nothing — terminal milestone.

## Goal

Give custom coding agents (and the Atelier examples) a tiny, stable
Python surface so they can adopt the compiler without going through
MCP. This is the API a non-MCP host writes against.

## Decision: monorepo or split package?

Two routes; ADR-002 picks one.

1. **Stay inside the monorepo.** Public surface lives at
   `atelier.prompt_compiler` (re-exports from
   `atelier.core.capabilities.prompt_compilation`). Versioned with the
   rest of Atelier. Simplest.
2. **Split into `atelier-prompt-compiler` on PyPI.** Same code, but
   with no dependency on the rest of Atelier core (we'd have to inline
   the pricing + tokens helpers). Useful if we want non-Atelier users
   to install just the compiler.

Default to (1) for the first cut. Re-evaluate after we have one
external user.

## Public surface

```python
from atelier.prompt_compiler import (
    PromptBlock,
    Stability,
    BlockKind,
    Provider,
    PromptCompiler,        # convenience class wrapping the capability
    LintReport,
    LintFinding,
)

compiler = PromptCompiler()        # uses default TelemetrySubstrate

compiled = compiler.compile(blocks, tail_budget_tokens=8000)
report   = compiler.lint(compiled, previous=prev_compiled)
rendered = compiler.render(compiled, provider=Provider.ANTHROPIC)

# After the host sends the request and gets a response:
compiler.attach_usage(
    trace_id=compiled.trace_id,
    usage=response.usage.to_dict(),
)
```

`PromptCompiler` is intentionally thin: it holds a reference to the
capability and exposes `compile`, `lint`, `render`, `attach_usage`.
Everything else is data classes.

## Convenience helpers

These ship with the SDK because every host writes them otherwise.

```python
from atelier.prompt_compiler.helpers import (
    tool_schema_block,        # from a JSON schema or function signature
    system_prompt_block,
    coding_policy_block,
    repo_summary_block,       # accepts the Atelier repo-summary dict
    reasonblock,              # accepts a ReasonBlock from the memory store
    file_summary_block,       # accepts an outline from CodeContextEngine
    user_task_block,
    git_diff_block,
    tool_result_block,
    scratchpad_block,
)
```

Each helper sets `kind`, the right default `Stability`, and a
deterministic `id` (e.g. `"tool_schema/" + sha256(json_schema)[:16]`).

## Examples

```
examples/prompt_compiler/
    minimal.py               # 30 lines: blocks → compile → render → print
    anthropic_loop.py        # full Anthropic turn loop with cache_control
    openai_loop.py           # OpenAI Responses API with prompt_cache_key
    reasonblock_integration.py  # pulls ReasonBlocks from `mcp__atelier__context`
```

The Anthropic / OpenAI examples are the ones we link from the README.

## Documentation

- `docs/agent-os/prompt-compiler.md` — usage guide, copy of the
  one-liner pitch from `index.md`, table of helpers, link to ADR-002.
- `docs/architecture/prompt-compiler.md` — internals (block model,
  sort key, hash chain, capability registry hookup).
- `docs/quality/scorecard.md` — extend with the cache-hit metrics from
  P5.

## Tests

- `test_sdk_surface.py::test_public_imports_exist`.
- `test_sdk_surface.py::test_compile_attach_usage_round_trip`.
- `test_helpers.py::test_helper_ids_deterministic`.
- `test_examples.py::test_minimal_example_runs_under_one_second`.

## Acceptance

- `from atelier.prompt_compiler import PromptCompiler` works in a fresh
  `uv run python` REPL.
- `examples/prompt_compiler/minimal.py` runs end-to-end.
- `make lint && make typecheck && make test` pass.

## Out of scope

- JS/TS SDK. Out of scope until we have a Node coding-agent user.
- A Rust SDK. Out of scope forever, until someone shows us a coding
  agent in Rust that needs sub-millisecond compile latency.
- Hosting the SDK behind an HTTP service. The Atelier FastAPI app
  (`core/service/api.py`) can pick this up later if traces show a use
  case; not in the first cut.
