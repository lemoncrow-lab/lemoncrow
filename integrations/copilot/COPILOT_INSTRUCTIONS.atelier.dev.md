## Atelier — Copilot Instructions


Atelier is the Agent Reasoning Runtime. Use the **3-step process**: call `context` to gather procedures, **Implement** the task with Atelier MCP tools first for file I/O, search, edits, and shell work, and call `record` at completion. Use native Copilot or VS Code tools only when Atelier returns `noop`, is hidden, or is unavailable; use `rescue` and `route` when needed.

Budget optimizer: before changing files, name the deliverable and summarize the smallest viable plan. Keep context narrow: use only the current goal, relevant files, failing command/output, and known constraints. Restate working context in under 10 bullets before editing or after compaction. If more than 10 minutes pass without an edit, name the expected deliverable or check with the user. If the same approach fails twice, call `rescue` or change approach; do not retry a third time.

All tools are available via MCP (server name: `atelier`). See `atelier/copilot/README.md` for details.

Use Atelier MCP tools as the default path for reads, search, edits, and shell
work. `read` and `search` are Atelier augmentations for bounded, repeated
context reads/searches. If an Atelier MCP tool returns `noop`, is hidden, or
is unavailable, use Copilot or VS Code native file reads, workspace search,
shell `rg`, or `grep`. Always return findings instead of waiting for tool
availability to improve.
