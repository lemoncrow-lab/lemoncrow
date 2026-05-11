# Trace Confidence Levels

Atelier assigns a `trace_confidence` level to every recorded trace. The level
describes what evidence is actually available for the run, so the proof gate
and cost/performance reports never claim better coverage than the host surface
provides.

## Levels

| Level          | Meaning                                                                                                                                          |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `full_live`    | Live hooks record prompt, tool, edit, command, compact, and stop events. Requires `hooks`, `live_hooks`, or `plugin_hooks` in `capture_sources`. |
| `mcp_live`     | Atelier MCP tool calls and tool outputs are captured; native host edits, commands, or compaction events may be incomplete.                       |
| `wrapper_live` | Wrapper captures task start/end and validation results, but not every native host event.                                                         |
| `imported`     | Host session data is imported after the run via a session importer (e.g., opencode SQLite DB or Claude JSONL).                                   |
| `manual`       | Agent calls `trace` with observable facts only. No live event stream is available.                                                               |

## Host Confidence Map

| Host        | Primary confidence          | Fallback confidence | Required capture_sources for primary | Missing surfaces when fallback               |
| ----------- | --------------------------- | ------------------- | ------------------------------------ | -------------------------------------------- |
| Claude Code | `full_live`                 | `mcp_live`          | `hooks` or `plugin_hooks`            | `hooks`, `bash_outputs`, `file_edits`        |
| Codex CLI   | `mcp_live` + `wrapper_live` | `manual`            | `mcp`, `wrapper`                     | `bash_outputs`, `file_edits`, `native_shell` |
| Copilot     | `mcp_live`                  | `manual`            | `mcp`                                | `native_chat_edits`, `file_edits`            |
| opencode    | `mcp_live` + `imported`     | `manual`            | `mcp`, `session_import`              | `native_events`                              |
| Gemini CLI  | `mcp_live`                  | `manual`            | `mcp`                                | `native_events`, `bash_outputs`              |

## Metadata Fields

Every trace carries four evidence fields:

- `host` — Derived from the `agent` string. One of `claude`, `codex`, `copilot`, `opencode`,
  `gemini`, or raw agent name if unrecognised.
- `trace_confidence` — One of the five levels above.
- `capture_sources` — List of active evidence channels for this trace
  (e.g. `["mcp", "hooks", "session_import"]`).
- `missing_surfaces` — List of host surfaces that were not captured
  (e.g. `["bash_outputs", "file_edits"]`).

## Proof-gate Rules

The cost/quality proof gate (`WP-32`) must not mark trace coverage as `full_live` unless:

1. `capture_sources` contains at least one of `hooks`, `live_hooks`, or `plugin_hooks`.
2. `missing_surfaces` does not contain `hooks`.

If a caller supplies `trace_confidence = "full_live"` without the required
`capture_sources`, Atelier automatically downgrades it to `mcp_live` and appends
`"hooks"` to `missing_surfaces`. This is enforced in `tool_record_trace` in
`src/atelier/gateway/adapters/mcp_server.py`.

## Usage

When calling `trace`, pass the new optional fields:

```json
&#123;
  "agent": "claude:claude-opus-4-5",
  "domain": "coding",
  "task": "Implement WP-30 trace confidence",
  "status": "success",
  "trace_confidence": "full_live",
  "capture_sources": ["hooks", "mcp"],
  "missing_surfaces": []
&#125;
```

If you do not supply `trace_confidence`, the field remains `null` on the
stored trace. Downstream reporting treats `null` as the worst-case level
(`manual`) for safety.
