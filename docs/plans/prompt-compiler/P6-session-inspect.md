# P6 — Session inspector (the killer workflow)

> Depends on: P2, P5.
> Unblocks: nothing (terminal milestone for the analyzer story).

## Goal

Replay an existing coding-agent session (Claude Code JSONL, Codex
session, or a generic OpenAI-shaped transcript) through the compiler
and tell the user **exactly why** their prefix isn't caching.

This is the wedge demo. Most prospective users do not yet have a
"compiled prompt"; they have a folder full of `.jsonl` files. P6 turns
those files into a diagnosis they can act on.

## Files

```
src/atelier/core/capabilities/prompt_compilation/
    session_importers/
        __init__.py
        claude_code.py
        codex.py
        generic.py
    diagnostics.py
src/atelier/gateway/adapters/cli_prompt.py    (wire up inspect-session)
tests/core/capabilities/prompt_compilation/
    test_session_importers.py
    test_diagnostics.py
tests/gateway/fixtures/prompt_compiler/
    sessions/claude_code_sample.jsonl
    sessions/codex_sample.jsonl
```

## Session importers

Each importer takes a path and yields a sequence of
`SessionTurn(blocks: list[PromptBlock], raw_usage: dict)`.

| Importer | Source format | Notes |
|---|---|---|
| `claude_code.py` | `~/.claude/projects/<workspace>/<session-uuid>.jsonl` | Reuses Atelier's existing Claude Code session reader — see `core/capabilities/optimization/` and `integrations/claude/` for the file shape. |
| `codex.py` | Codex session export (OpenAI shape) | Maps system/user/tool messages to block kinds heuristically. |
| `generic.py` | A JSONL we document in the README: each line is `{role, content, tool_schemas?}`. | Escape hatch for everything else. |

Each importer is best-effort: when it can't classify a message
confidently, it falls back to `BlockKind.TOOL_RESULT` (turn stability)
and adds a metadata flag so the diagnostics can mention it.

## Diagnostics output

```
Cache diagnosis for session abc123 (claude_code, 27 turns)

Stable prefix candidates (per-turn average):
  - Tool schemas: 3,200 tokens
  - System prompt: 1,100 tokens
  - ReasonBlocks: 2,400 tokens
  - Repo summary: 1,700 tokens

Cache breakers found across the session:
  1. content.timestamp-in-prefix    18 turns  (first seen turn 1)
  2. ordering.volatile-before-stable 11 turns
  3. tools.reordered-since-previous  6 turns
  4. content.raw-log-in-turn         9 turns

Recommended layout (one-line summary):
  [tools] [system] [coding policy] [repo summary] [ReasonBlocks] [task] [diff] [tool results]

Estimated improvement:
  Current cacheable prefix:   1,100 tokens  (only system survives breakers)
  Optimized cacheable prefix: 8,400 tokens
  Estimated savings @ provider: $X.YY / hour of work
```

The estimate uses `core/capabilities/pricing.py` and a documented
assumption (turns per hour × tokens per turn × cached-vs-uncached
delta). Print the assumption alongside the number — don't fake
precision.

## CLI

```
atelier prompt inspect-session ~/.claude/projects/.../session.jsonl \
    [--from claude|codex|generic] \
    [--format text|json] \
    [--out FILE]
```

Default `--from` is auto-detected from file shape; pass explicitly when
detection fails.

## Tests

- `test_session_importers.py::test_claude_code_sample_parses`.
- `test_session_importers.py::test_codex_sample_parses`.
- `test_session_importers.py::test_generic_fallback`.
- `test_diagnostics.py::test_breaker_counts_match_fixture`.
- `test_diagnostics.py::test_estimated_improvement_uses_pricing_module`.

Fixtures (`claude_code_sample.jsonl`, `codex_sample.jsonl`) include
hand-crafted poisoned prompts so the breaker counts are
golden-testable.

## Acceptance

- `uv run atelier prompt inspect-session tests/gateway/fixtures/prompt_compiler/sessions/claude_code_sample.jsonl`
  produces the diagnosis shown above (modulo numbers).
- The breaker list survives a round trip through `--format json`.

## Out of scope

- Live monitoring of an in-progress session. The Claude Code plugin
  hooks (`integrations/claude/plugin/hooks/`) can pick this up later
  once the diagnostic surface is stable.
- A web UI. The CLI output is the surface for now; the JSON shape is
  the contract a dashboard would later read.
