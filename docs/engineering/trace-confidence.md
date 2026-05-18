# Trace Confidence

Atelier stores trace provenance explicitly so agents and maintainers can decide
how much to trust a recorded session. Every trace may carry the metadata fields
`host`, `trace_confidence`, `capture_sources`, and `missing_surfaces`.

## Confidence Levels

| trace_confidence | Meaning | Typical capture_sources | Typical missing_surfaces |
| --- | --- | --- | --- |
| `full_live` | Live host hooks and Atelier capture both saw the session end-to-end. | `["hooks", "mcp"]` | `[]` |
| `mcp_live` | Atelier MCP saw the tool calls live, but host-native actions may still be missing. | `["mcp"]` | host-native edits, shell output, UI actions |
| `wrapper_live` | A shell/task wrapper observed execution boundaries but not every tool call. | `["wrapper"]` | detailed tool payloads, native edits |
| `imported` | The trace was reconstructed from host exports or imported session artifacts after the fact. | `["import"]` | live hooks, precise timing, transient UI state |
| `manual` | The agent or operator recorded the trace explicitly from observed facts. | `["manual"]` | everything not stated by the recorder |

## Host Mapping

| host | trace_confidence | capture_sources | missing_surfaces | Notes |
| --- | --- | --- | --- | --- |
| Claude Code | `full_live`, `mcp_live`, `imported`, `manual` | hooks, mcp, import, manual | native IDE state when only imported/manual | Claude is the only host that can plausibly reach `full_live` because plugin hooks exist. |
| Codex CLI | `mcp_live`, `wrapper_live`, `imported`, `manual` | mcp, wrapper, import, manual | native shell/file edits outside MCP, missing hook stream | Codex has no durable hook bus today, so `full_live` is not a valid claim. |
| Copilot | `mcp_live`, `wrapper_live`, `manual` | mcp, wrapper, manual | native chat edits, VS Code file edits, hidden task internals | Copilot tasks help with wrappers, but they do not provide full per-tool hooks. |
| opencode | `mcp_live`, `imported`, `manual` | mcp, import, manual | stable hook coverage, some local editor actions | DB import improves recovery, but live provenance is thinner than Claude. |
| Gemini CLI | `mcp_live`, `imported`, `manual` | mcp, import, manual | native CLI-only actions, missing hook stream | Saved chats can be imported, but there is no first-class hook surface. |

## Recording Rules

1. Always set `host` when the originating environment is known.
2. Set `trace_confidence` to the strongest truthful level only.
3. Populate `capture_sources` with the concrete evidence path: `hooks`, `mcp`, `wrapper`, `import`, or `manual`.
4. Populate `missing_surfaces` with the important blind spots that remain.
5. Never claim `full_live` unless hooks are present in `capture_sources`.
