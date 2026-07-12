# LemonCrow MCP

LemonCrow's MCP server is the host-neutral transport for context retrieval,
rescue, trace recording, rubric gates, memory, and code-aware helper tools.

## Start Modes

### Installed product

```bash
lc mcp
```

### Source checkout

```bash
cd lemoncrow
uv run lc mcp
```

### Remote service-backed mode

Set `LEMONCROW_SERVICE_URL` (plus `LEMONCROW_API_KEY` if the service requires auth)
to route the supported core calls through the HTTP service. Remote mode is
enabled whenever `LEMONCROW_SERVICE_URL` is set; unset it to run fully local.

## Active vs. Passive Mode

LemonCrow distinguishes between developer mode and passive compatibility mode.

- With `LEMONCROW_DEV_MODE=1`, the stdio MCP server exposes the full active tool surface.
- Without developer mode, `record` remains active and the host may still see some
  compatibility tools as passive `noop` surfaces, but context/retrieval/edit
  workflows are intentionally not active.
- Older host templates may use previous-generation tool names; the list below is
  the current public stdio MCP surface.

## Active Tool Names

With `LEMONCROW_DEV_MODE=1`, the current stdio MCP registry exposes these tool
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

In remote MCP mode, LemonCrow currently forwards the core service-backed flows for:

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
    "lc": {
      "command": "lc mcp",
      "env": {
        "LEMONCROW_ROOT": "~/.lemoncrow",
        "LEMONCROW_WORKSPACE_ROOT": "."
      }
    }
  }
}
```

Source-checkout config:

```json
{
  "mcpServers": {
    "lc": {
      "command": "uv",
      "args": ["run", "lc mcp"],
      "cwd": "/abs/path/to/lemoncrow",
      "env": {
        "LEMONCROW_ROOT": "~/.lemoncrow",
        "LEMONCROW_WORKSPACE_ROOT": "."
      }
    }
  }
}
```

## Embedding via SDK

When you want the MCP contract in-process, use `LemonCrowClient.mcp()` from the
Python SDK. It mirrors the same tool semantics and dev-mode gating rules as the
stdio server.
