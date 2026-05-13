---
name: task
description: Use this skill at the start of every coding task. It records the task and retrieves Atelier context so you do not repeat known dead ends.
---

# Task

When this skill activates, call the MCP tool:

```
task({
  task: "<one-sentence task>",
  domain: "<domain or null>",
  files: ["<likely files>"],
  tools: ["<likely tools>"],
  errors: ["<known error messages>"]
})
```

Read every returned ReasonBlock. They are short on purpose.

Then draft and execute the smallest viable plan. If the same test or command fails twice with the same error signature, call `rescue` before retrying. Record the observable outcome with `trace`.

Never include secrets, API keys, tokens, or hidden chain-of-thought.
