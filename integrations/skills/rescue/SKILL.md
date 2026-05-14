---
name: rescue
description: Use when a command, test, or tool call has failed twice with the same error signature, or when you find yourself stuck in a loop. Atelier will surface relevant rescue procedures from past incidents.
---

# Rescue

Part of the **Implement** phase. The third attempt at a failing command rarely succeeds without changing
something. This skill consults the Atelier reasoning store for relevant
recovery procedures.

## Trigger

Activate when **any of these** is true:

- The same shell command failed twice with the same exit code / error.
- The same pytest / vitest test failed twice with the same assertion.
- The same MCP tool call returned the same error twice.
- You have made >3 edits to the same file without progress.

## Action

```
rescue({
  task: "<one sentence task>",
  error: "<copy the error message verbatim, redacted of secrets>",
  files: ["<files in play>"],
  domain: "<if known>",
  recent_actions: ["last 3-5 things you tried"]
})
```

## Use the result

The response includes:
- `rescue` — a short procedure to follow.
- `matched_blocks` — ReasonBlock ids the rescue is drawn from.

Apply the rescue **before** running the failing thing again. Do not
ignore the response and retry blindly.

## Hard rules

- Never include secrets, tokens, or API keys in `error` or
  `recent_actions`.
- Never call this skill more than 3 times for the same root cause —
  escalate to the user instead.
