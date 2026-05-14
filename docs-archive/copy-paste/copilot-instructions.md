# Copilot Instructions Block (Copy-Paste)

Use this page when you cannot rely on the packaged Copilot integration and need
either a plain instruction block or a hand-written `.vscode/mcp.json` entry.

## Minimal Block

```markdown
## Atelier Runtime Contract

Before editing files:

1. Start from a concrete anchor such as a failing command, file, symbol, or nearby implementation surface.
2. Gather only enough local context to form one falsifiable hypothesis before the first edit.
3. For state-changing work, resolve and record canonical identifiers before writing.

After the first substantive edit:

1. Run the cheapest focused validation before widening scope.
2. If the same failure repeats, switch to a rescue path instead of retrying blindly.
3. Record only observable outcomes: commands run, files touched, validations, and errors.

Never store hidden chain-of-thought or secrets in summaries.
```

## MCP-Enabled Block

If Copilot Chat can access the `atelier` MCP server, add this guidance:

```markdown
## Atelier Runtime Contract (MCP)

When the runtime is in developer mode (`ATELIER_DEV_MODE=1`):

1. Call `context` before risky work that needs prior procedures or constraints.
2. Use `search` and `read` to gather only the minimum local evidence needed.
3. Call `rescue` after repeated failures.
4. Call `verify` for rubric-gated domains.
5. Call `trace` with an observable task summary before finishing.

If a tool returns `noop`, the runtime is in passive mode. `trace` remains the most reliable always-on surface.
```

## Setup for `.vscode/mcp.json`

Installed-product configuration:

```json
{
  "servers": {
    "atelier": {
      "type": "stdio",
      "command": "atelier-mcp",
      "env": {
        "ATELIER_WORKSPACE_ROOT": "${workspaceFolder}"
      }
    }
  }
}
```

Source-checkout configuration:

```json
{
  "servers": {
    "atelier": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "${workspaceFolder}/atelier", "atelier-mcp"],
      "env": {
        "ATELIER_WORKSPACE_ROOT": "${workspaceFolder}"
      }
    }
  }
}
```
