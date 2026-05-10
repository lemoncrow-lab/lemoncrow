---
description: Show the current Atelier run ledger — plan, verified facts, open questions, blockers, recent alerts.
argument-hint: "[run_id]"
---

Show the Atelier run status.

If `$1` is provided, treat it as the `run_id`. Otherwise, use the most
recent run.

1. Call `reasoning({ run_id: "$1" })`. If `$1` is empty,
   omit the argument and let the server resolve the latest run.
2. If the tool returns an error like "no run ledger found", reply:
   > No active run. Start a task with the `atelier:code` agent to create one.
3. Otherwise format the response as:
   - **Run**: `<run_id>` (`<agent>` / `<status>`)
   - **Task**: `<task>`
   - **Domain**: `<domain or none>`
   - **Plan**: numbered list from `current_plan`.
   - **Verified facts**: bullets from `verified_facts`.
   - **Open questions**: bullets from `open_questions`.
   - **Blockers**: bullets from `current_blockers`.
   - **Tool calls**: `<tool_count>` · **Tokens**: `<token_count>`.
   - **Recent alerts**: last 5 `watchdog_alert` events with severity.

Do not invent fields. If a list is empty, write `(none)`.
