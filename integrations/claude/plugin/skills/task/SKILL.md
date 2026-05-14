---
description: "Use when: starting or coordinating an Atelier task loop from context retrieval through trace recording."
allowed-tools: "mcp__atelier__context, mcp__atelier__rescue, mcp__atelier__verify, mcp__atelier__trace"
---

Run the Atelier task loop.

1. Call `context` with task, files, domain, tools, and errors.
2. Draft a short plan using the returned procedures.
3. Use `rescue` after repeated identical failures.
4. Use `verify` for required rubrics.
5. Call `trace` at completion with observable facts only.

Keep the loop explicit and concise.
