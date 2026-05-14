---
name: task
description: Use this skill at the start of every coding task. It records the task and retrieves Atelier context so you do not repeat known dead ends.
---

# Task

Atelier uses a **3-step process** for reliable coding:

1.  **Gather Context**: Call the `context` tool with your task details. Read the returned ReasonBlocks and avoid known dead ends.
2.  **Implement**: Execute the task. Use `rescue` if a command or tool fails twice with the same error signature. Use `route` for complex decisions.
3.  **Trace**: Call the `trace` tool at completion to record the observable outcome.

## Step 1: Gather Context

Call the MCP tool:

```
context({
  task: "<one-sentence task>",
  domain: "<domain or null>",
  files: ["<likely files>"],
  tools: ["<likely tools>"],
  errors: ["<known error messages>"]
})
```

Read every returned ReasonBlock. They are short on purpose.

Never include secrets, API keys, tokens, or hidden chain-of-thought.
