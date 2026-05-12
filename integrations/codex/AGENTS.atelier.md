# Atelier — Codex Agent


Atelier is the Agent Reasoning Runtime. Before every coding task, call
`reasoning` with your task, domain, and tools. Before executing a plan, call
`lint` — status `blocked` means a known dead end was detected. On failure,
call `rescue`. After completing a task, call `trace`.

Budget optimizer: before changing files, name the deliverable and summarize
the smallest viable plan. Keep context narrow: use only the current goal,
relevant files, failing command/output, and known constraints. Restate working
context in under 10 bullets before editing or after compaction. If more than
10 minutes pass without an edit, check with the user. If the same approach
fails twice, call `rescue` or change approach; do not retry a third time.

All tools are available via MCP server name `atelier`.
