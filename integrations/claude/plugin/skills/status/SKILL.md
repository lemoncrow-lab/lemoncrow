---
description: "Use when: showing current Atelier run status, run ledger, plan, facts, blockers, or recent alerts."
allowed-tools: "mcp__atelier__task"
---

Show the current Atelier run status.

1. Call `task` with the current task if it is known.
2. Render the task, domain, and most relevant returned procedures.
3. If no run ledger exists, say there is no active run and suggest starting work with `atelier:code`.

Do not invent missing fields.
