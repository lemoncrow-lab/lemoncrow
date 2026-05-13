---
description: "Use when: analyzing repeated failures, failed tests, stuck agent loops, or rescue recommendations."
allowed-tools: "mcp__atelier__rescue, mcp__atelier__task"
---

Analyze a failure and suggest recovery.

1. Collect the task, failing command, error signature, files, and recent actions.
2. Call `rescue` when the same failure repeated or the user says the run is stuck.
3. Return the rescue procedure and the next two concrete actions.

Do not rerun the same failing command before applying the rescue advice.
