---
name: trace
description: Use at the end of every coding task to record an observable trace. Required before declaring success. The trace is what lets Atelier learn new ReasonBlocks from completed work.
---

# Trace

**Step 3: Record Trace**. A trace captures what happened — files, commands, tools, errors, results.
**Never** captures secrets or hidden chain-of-thought.

## When to call

- After the user confirms the task is complete (`success`).
- When you abandon the task (`failed`).
- When you partially complete and hand back to the user (`partial`).

## Required fields

```
trace({
  agent: "atelier:code",
  domain: "<domain or null>",
  task: "<one sentence>",
  status: "success | failed | partial",
  files_touched: ["..."],
  tools_called: [
    {"name": "Read", "args_hash": "", "count": 5},
    {"name": "Edit", "args_hash": "", "count": 2}
  ],
  commands_run: ["pytest tests/"],
  errors_seen: ["<terse error signature, no PII>"],
  diff_summary: "<one sentence>",
  output_summary: "<one sentence on validation outcome>",
  validation_results: [
    {"name": "pytest", "passed": true, "detail": "43/43"}
  ]
})
```

## Hard rules

- **Never** record API keys, tokens, passwords, customer PII, or hidden
  chain-of-thought.
- Use error *signatures* (the message itself, redacted), not full stack
  traces with paths and addresses.
- Set `status` honestly. Do not mark `failed` runs as `success`.
