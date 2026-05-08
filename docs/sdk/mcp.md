# Atelier MCP

Atelier's MCP server is the host-neutral way to plug procedural reasoning, rubric gates, failure rescue, compacting, smart file operations, and agent memory into existing agent CLIs.

## Start Modes

### Local stdio

```bash
cd atelier
uv run atelier-mcp
```

### Remote service-backed mode

Set `ATELIER_MCP_MODE=remote` plus `ATELIER_SERVICE_URL` and `ATELIER_API_KEY` to route supported core calls through the HTTP service.

## Agent-Facing Tools

The MCP registry exposes exactly these prefixed tool names:

- `reasoning`
- `lint`
- `route`
- `rescue`
- `trace`
- `verify`
- `memory`
- `read`
- `edit`
- `search`
- `compact`
- `atelier_repo_map`

There are no unprefixed aliases. Use CLI commands for governance and admin workflows such as `atelier lesson inbox`, `atelier consolidation decide`, `atelier report`, `atelier sql inspect`, `atelier proof show`, and `atelier route contract`.

## Dispatch Surfaces

`memory` uses an `op` field for `block_upsert`, `block_get`, `archive`, `recall`, and `summarize`.

`compact` uses an `op` field for `output`, `session`, and `advise`.

`route` uses an `op` field for `decide` and `verify`.

## Host Example

```json
{
  "mcpServers": {
    "atelier": {
      "command": "uv",
      "args": ["run", "atelier-mcp"],
      "env": {
        "ATELIER_ROOT": "~/.atelier",
        "ATELIER_WORKSPACE_ROOT": "."
      }
    }
  }
}
```

## Embedding via SDK

When you want the MCP contract in-process, use `AtelierClient.mcp()` from the Python SDK. It uses the same tool semantics and can run in loopback mode for tests and embedded agents.
