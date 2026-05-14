# Tool Supervision

Capability path:
- `src/atelier/core/capabilities/tool_supervision/`

## Purpose

Tool supervision reduces waste by detecting redundant operations and caching observations.

## Behavior

- tracks total tool calls
- tracks avoided calls via cache hits
- tracks retries prevented
- stores reusable observations in local state

## Runtime API

- `AtelierRuntimeCore.smart_search(...)`
- `AtelierRuntimeCore.smart_edit(...)`
- MCP: `search`, `search`, `edit`
