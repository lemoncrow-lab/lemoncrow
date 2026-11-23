# Protocol / MCP reference

> **Integration-author reference.** Normal users should use the LemonCrow plugin/integration instructions for their coding agent. You do not need to start or manage an MCP server for normal Workspaces use.

This page documents the underlying Model Context Protocol transport used by host adapters and the explicit remote-workspace feature.

## Local protocol adapter

Run the low-level adapter directly with:

```bash
lc runtime mcp
```

`lc rt mcp` is the short alias.

For source-checkout development:

```bash
uv run lc runtime mcp
```

Use diagnostics rather than manually speaking JSON-RPC when checking an installation:

```bash
lc rt mcp check --json
lc rt mcp stats
```

## Core visible tool surface

The normal model-facing core allowlist is intentionally small:

- `bash`
- `code_search`
- `edit`
- `read`
- `web_fetch`

Registration does not imply visibility. The allowlist in `src/lemoncrow/core/environment.py` is the authority for the standard public surface, and product-specific profiles can narrow it further.

Review Ask and other trust boundaries use narrower profiles where appropriate.

## Host configuration shape

A host that accepts executable + argument arrays should configure LemonCrow like this:

```json
{
  "mcpServers": {
    "lc": {
      "command": "lc",
      "args": ["runtime", "mcp", "--host", "example-host"],
      "env": {
        "LEMONCROW_WORKSPACE_ROOT": "/absolute/path/to/repo"
      }
    }
  }
}
```

For a source checkout using `uv`:

```json
{
  "mcpServers": {
    "lc": {
      "command": "uv",
      "args": ["run", "lc", "runtime", "mcp", "--host", "example-host"],
      "cwd": "/absolute/path/to/lemoncrow",
      "env": {
        "LEMONCROW_WORKSPACE_ROOT": "/absolute/path/to/repo"
      }
    }
  }
}
```

Actual host installers may use host-specific keys, absolute executable paths, plugin bundles, hooks, or additional environment values. Prefer the host installer over hand-written config.

## Workspaces Lite host adapter

Claude Code, Codex, and OpenCode use a lighter local adapter for their normal local Workspaces plugin path. Their installers configure the appropriate Workspaces Lite entry point automatically; users should not reproduce that configuration manually.

Cursor likewise has a dedicated installer that owns its config and lifecycle hooks.

See [`docs/hosts/`](../hosts/all-agent-clis.md) for the supported integration paths.

## Remote MCP

Remote MCP is a separate, explicit feature for publishing the current workspace to compatible remote clients such as ChatGPT, Claude, Cursor, VS Code, or Zed.

```bash
lc rt mcp serve
```

The command serves the streamable-HTTP MCP endpoint at `/mcp` behind OAuth and can create a public tunnel when requested by its options.

This feature is deliberately called **Remote MCP** because here the protocol/server boundary is the feature. It should not be used as the mental model for the normal local coding-agent plugin.

Inspect the exact options with:

```bash
lc rt mcp serve --help
```

## Managed connectors

For persistent remote MCP connectors/tunnels:

```bash
lc rt mcp service --help
```

For a stable OAuth client identifier when a remote client asks for one:

```bash
lc rt mcp client
```

## Protocol diagnostics

Useful commands:

```bash
lc rt mcp check --json
lc rt mcp list
lc rt mcp debug --help
lc rt mcp stats
```

These are integration/debug surfaces. They are not required for normal local Workspaces usage.

## SDK embedding

When an integration needs LemonCrow in-process rather than through a host protocol process, use the Python client/SDK surfaces documented in [Python SDK](python.md). Keep the product tool visibility/trust policy intact instead of constructing an independent tool list.
