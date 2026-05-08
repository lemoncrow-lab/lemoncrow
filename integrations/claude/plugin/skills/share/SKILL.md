---
description: "Use when: sharing an Atelier run summary, trace, report, or concise status update."
allowed-tools: "mcp__atelier__trace, Bash(atelier trace *)"
---

Prepare a shareable Atelier summary.

1. Identify the current or requested run id.
2. If needed, inspect the trace with `atelier trace show <run_id>`.
3. Summarize task, files touched, validation, and outcome in a compact shareable block.

Do not include secrets, tokens, hidden reasoning, or raw private logs.
