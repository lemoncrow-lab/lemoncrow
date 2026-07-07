# Atelier MCP

Atelier's MCP server is the host-neutral transport for context retrieval,
rescue, trace recording, rubric gates, memory, and code-aware helper tools.

## Start Modes

### Installed product

```bash
atelier mcp
```

### Source checkout

```bash
cd atelier
uv run atelier mcp
```

### Remote service-backed mode

Set `ATELIER_MCP_MODE=remote` plus `ATELIER_SERVICE_URL` and `ATELIER_API_KEY`
to route the supported core calls through the HTTP service.

## Active vs. Passive Mode

Atelier distinguishes between developer mode and passive compatibility mode.

- With `ATELIER_DEV_MODE=1`, the stdio MCP server exposes the full active tool surface.
- Without developer mode, `record` remains active and the host may still see some
  compatibility tools as passive `noop` surfaces, but context/retrieval/edit
  workflows are intentionally not active.
- Older host templates may use previous-generation tool names; the list below is
  the current public stdio MCP surface.

## Active Tool Names

With `ATELIER_DEV_MODE=1`, the current stdio MCP registry exposes these tool
names:

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
- `bash`

## Structured Tool Operations

- `memory` uses an `op` field such as `block_upsert`, `block_get`, `archive`, and `recall`.
- `compact` uses `op=output`, `op=session`, or `op=advise`.
- `route` uses `op=decide` and `op=verify`.
- `code` uses `op=index`, `op=search`, `op=blame`, `op=hover`, `op=symbol`, `op=outline`, `op=files`, `op=explore`, `op=routes`, `op=status`, `op=context`, `op=pattern`, `op=rename`, `op=cache_status`, or `op=cache_invalidate`. (Call-graph and reference relations — callers, callees, usages — are returned folded into `op=explore`.)

## Remote Mode Coverage

In remote MCP mode, Atelier currently forwards the core service-backed flows for:

- context retrieval via `/v1/reasoning/context`
- rescue via `/v1/reasoning/rescue`
- rubric evaluation via `/v1/rubrics/run`
- trace recording via `/v1/traces`
- memory block/archive/recall operations via `/v1/memory/*`

## Host Example

Installed-product config:

```json
{
  "mcpServers": {
    "atelier": {
      "command": "atelier mcp",
      "env": {
        "ATELIER_ROOT": "~/.atelier",
        "ATELIER_WORKSPACE_ROOT": "."
      }
    }
  }
}
```

Source-checkout config:

```json
{
  "mcpServers": {
    "atelier": {
      "command": "uv",
      "args": ["run", "atelier mcp"],
      "cwd": "/abs/path/to/atelier",
      "env": {
        "ATELIER_ROOT": "~/.atelier",
        "ATELIER_WORKSPACE_ROOT": "."
      }
    }
  }
}
```

## Embedding via SDK

When you want the MCP contract in-process, use `AtelierClient.mcp()` from the
Python SDK. It mirrors the same tool semantics and dev-mode gating rules as the
stdio server.
