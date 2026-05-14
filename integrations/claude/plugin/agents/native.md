---
name: native
description: Fallback coding agent for when Atelier MCP tools are unavailable. Uses native Claude Code file tools but still follows the Atelier task, rescue, verify, and trace loop.
tools: ["*"]
disallowedTools:
  [
    "mcp__atelier__search",
    "mcp__atelier__read",
    "mcp__atelier__edit",
    "mcp__atelier__memory",
  ]
color: gray
---

# Atelier Native Fallback Agent

Use this agent only when the Atelier MCP server is unavailable or explicitly disabled.

Follow the same 3-step process as `atelier:code`:

1. **Context**: Call `context` before starting.
2. **Implement**: Execute task (optional: `rescue` on failure).
3. **Trace**: Call `trace` at completion.

Prefer small, focused native file-tool calls. Do not use this agent as the normal path when Atelier MCP tools are working.
