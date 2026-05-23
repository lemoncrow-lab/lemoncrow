# P7 — MCP tool integration

> Depends on: P1, P3 (rendering shape must be stable before we expose it).
> Unblocks: P8 (SDK reuses the same handler).

## Goal

Let agents using the Atelier MCP server invoke the compiler without
shipping the Python SDK. One round-trip in, compiled prompt + cache
metadata out.

## Decision: extend `compact` or add a new `prompt` tool?

Both options are live; the milestone picks one **before** writing
code. The bias is toward **a new `prompt` tool** because the surfaces
of "compact a conversation" and "compile a list of typed blocks" are
genuinely different. Final call lives in
`docs/decisions/002-prompt-compiler.md`.

### Option A — extend `compact`

```python
@mcp_tool(name="compact")
def tool_compact(op: Literal["...", "prompt_compile"], ...):
    if op == "prompt_compile":
        return _do_prompt_compile(...)
```

Pros: no new tool. Cons: `compact`'s args (`content`, `session_id`,
`complexity`) don't fit the compiler's inputs.

### Option B — new top-level `prompt` tool

```python
@mcp_tool(name="prompt", is_dev=True)
def tool_prompt(
    op: Literal["compile", "lint", "inspect_session", "attach_usage"],
    blocks: list[dict] | None = None,
    session_path: str | None = None,
    provider: Literal["openai","anthropic","gemini","deepseek"] | None = None,
    tail_budget_tokens: int | None = None,
    previous_compile_id: str | None = None,
    trace_id: str | None = None,
    usage: dict | None = None,
) -> dict: ...
```

This milestone assumes Option B unless ADR-002 reverses it.

## Ops

| `op` | Inputs | Returns |
|---|---|---|
| `compile` | `blocks`, optional `provider`, optional `tail_budget_tokens` | `{compiled, rendered?, trace_id}` |
| `lint` | `blocks`, optional `previous_compile_id` | `{report: LintReport.to_dict()}` |
| `inspect_session` | `session_path`, optional `from` | `{diagnosis: ...}` |
| `attach_usage` | `trace_id`, `usage` | `{updated: bool, cache_read_tokens, cache_hit_rate, savings_usd}` |

All ops return JSON-safe dicts. `attach_usage` is the only op that
mutates DB rows; the rest are read-mostly.

## Authorization

Same as the rest of the MCP surface — no extra auth.

## Files

```
src/atelier/gateway/adapters/mcp_server.py     (register tool_prompt)
src/atelier/gateway/adapters/mcp_prompt.py     (handler module, mirrors
                                                tool_compact pattern)
tests/gateway/test_p0_mcp_surfaces.py          (extend with prompt-tool cases)
tests/gateway/test_mcp_tool_handlers.py        (extend)
```

## Tests

- `test_p0_mcp_surfaces.py::test_prompt_tool_registered`.
- `test_mcp_tool_handlers.py::test_prompt_compile_round_trip`.
- `test_mcp_tool_handlers.py::test_prompt_lint_returns_findings`.
- `test_mcp_tool_handlers.py::test_attach_usage_updates_trace_row`.

Run via:

```
uv run pytest tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py -q
```

(as documented in `CLAUDE.md` under "Validation by Change Surface").

## Acceptance

- The MCP tool list (`mcp__atelier__prompt`) shows up when an agent
  introspects the server.
- A round-trip from a host agent (Claude Code via the Atelier plugin)
  hits the tool and returns compiled output.
- Plugin reinstall (`bash scripts/install_claude.sh`) picks up the new
  tool automatically.

## Out of scope

- Streaming responses. The compiler is fast enough that streaming adds
  no value.
- A long-poll mode for `inspect_session` on huge transcripts. If a
  session is too large to compile in one request, the host can chunk it
  and call `attach_usage` per turn.
