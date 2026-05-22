# P4 — CLI surface

> Depends on: P1, P2, P3.
> Unblocks: P6 (uses these commands as building blocks).

## Goal

Ship `atelier prompt …` as the first user-visible thing. The CLI is a
thin shell over the capability — anything the CLI does must be a single
function call on `PromptCompilerCapability`.

## Files

```
src/atelier/gateway/adapters/cli.py            (extend with a `prompt` Click group)
src/atelier/gateway/adapters/cli_prompt.py     (the new command module)
tests/gateway/test_cli_prompt.py
```

## Commands

```
atelier prompt compile  <blocks.json>   [--provider openai|anthropic|gemini|deepseek]
                                        [--tail-budget-tokens N]
                                        [--out FILE]
atelier prompt lint     <blocks.json>   [--previous prev_blocks.json]
                                        [--format text|json]
atelier prompt inspect-session <PATH>   [--from claude|codex|generic]
                                        [--format text|json]
                                        (deferred body to P6; P4 stubs --help)
```

### `compile`

Reads a JSON file of the form:

```json
{
  "blocks": [
    {"id": "tools.v1",   "kind": "tool_schema",   "content": "..." },
    {"id": "sys.v1",     "kind": "system",        "content": "..." },
    {"id": "user.t12",   "kind": "user_task",     "content": "..." }
  ],
  "tail_budget_tokens": 8000,
  "provider": "anthropic"
}
```

Prints (or writes to `--out`) a JSON object containing:

- `compiled`: blocks in compile order with stability + token estimates.
- `stable_prefix_hash`, `stable_prefix_tokens`, `dynamic_tail_tokens`.
- `rendered`: the provider-shaped body when `--provider` is set.

### `lint`

Loads blocks, compiles, lints, and prints either:

- `text` (default) — a human-readable report:

  ```
  Cache score: 82 / 100
  Stable prefix: 8,420 tokens
  Volatile tokens before prefix end: 0
  Estimated OpenAI cached-token eligibility: yes
  Estimated Anthropic breakpoint: after block reasonblocks/team-rules

  Findings:
    [ERROR] ordering.volatile-before-stable (block scratchpad/main)
            Scratchpad appears at index 2; last stable block is at index 5.
            fix: pass `tail_budget_tokens` so the compiler can sort.
    [WARN]  size.prefix-below-openai-floor
            Stable prefix is 768 tokens; OpenAI cache won't activate.
  ```

- `json` — machine-readable `LintReport.to_dict()`.

`--previous` lets the user compare against an earlier compile to catch
tool-order drift.

### `inspect-session`

Stub in P4 (`raise click.ClickException("implemented in P6")`); the
full implementation lands in P6 once session importers are wired.

## Tests

- `test_cli_prompt.py::test_compile_writes_expected_shape`.
- `test_cli_prompt.py::test_lint_text_format_human_readable` (assert
  certain substrings exist).
- `test_cli_prompt.py::test_lint_json_format_round_trips`.
- `test_cli_prompt.py::test_compile_with_provider_emits_rendered_body`.
- `test_cli_prompt.py::test_invalid_json_returns_clear_error`.

CLI tests use Click's `CliRunner`. Fixtures live in
`tests/gateway/fixtures/prompt_compiler/`.

## Acceptance

- `uv run atelier prompt --help` lists `compile`, `lint`,
  `inspect-session`.
- `uv run atelier prompt lint tests/gateway/fixtures/prompt_compiler/clean.json`
  prints "Cache score: 100 / 100" and exits 0.
- `uv run atelier prompt lint tests/gateway/fixtures/prompt_compiler/poisoned.json`
  exits non-zero with at least one ERROR.

## Out of scope

- TUI / interactive mode.
- A `watch` subcommand (it's tempting; we can add it after P6 if traces
  show a need).
