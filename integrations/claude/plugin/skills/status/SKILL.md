---
description: "Use when: showing current Atelier run status, run ledger, plan, facts, blockers, or recent alerts."
allowed-tools: "mcp__atelier__reasoning"
---

Show the current Atelier run status.

1. Call `reasoning`. If the user supplied a run id, pass it as `run_id`.
2. Render the task, domain, current plan, verified facts, open questions, blockers, tool count, token count, and recent alerts.
3. If no run ledger exists, say there is no active run and suggest starting work with `atelier:code`.

Do not invent missing fields.
