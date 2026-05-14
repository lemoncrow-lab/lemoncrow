# Core Capabilities

Atelier core capabilities live at:

- `src/atelier/core/capabilities/`

## Capability Set

1. `context_reuse`
2. `semantic_file_memory`
3. `loop_detection`
4. `tool_supervision`
5. `context_compression`

These capabilities are internal and runtime-managed. Agent code and host adapters remain thin.

## Runtime Exposure

CLI:

- `atelier capability list`
- `atelier capability status`

MCP tools:

- `context`
- `route`
- `rescue`
- `trace`
- `verify`
- `memory`
- `read`
- `edit`
- `sql`
- `search`
- `compact`
- `code`
- `shell`

CLI-only workflows include `atelier lesson inbox`, `atelier consolidation inbox`, `atelier report`, `atelier proof show`, and `atelier route contract`.
