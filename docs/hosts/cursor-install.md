# Installing Atelier into Cursor IDE

**Support level**: MCP server (stdio JSON-RPC)

---

## Quick Install

```bash
make install
```

By default this installs Cursor user/global MCP config. For a project-local install:

```bash
bash scripts/install_cursor.sh --workspace /path/to/workspace
```

---

## What Gets Installed

| Artifact          | Global install                         | `--workspace DIR` install              |
| ----------------- | -------------------------------------- | -------------------------------------- |
| MCP server config | `~/.cursor/mcp.json`                   | `<workspace>/.cursor/mcp.json`         |
| Rules (optional)  | (none — Cursor rules are project-only) | `<workspace>/.cursor/rules/*.mdc`|

The installer merges an `atelier` entry into the `mcpServers` key:

```json
{
  "mcpServers": {
    "atelier": {
      "type": "stdio",
      "command": "atelier-mcp",
      "args": ["--host", "cursor"]
    }
  }
}
```

For global installs, Cursor's working directory for MCP subprocesses is **not** the
workspace root, so we inject `args` that handle workspace resolution automatically.
The `--host cursor` flag tells Atelier's MCP server which agent environment it's
running in, enabling correct trace labeling.

### Cursor Rules (`.cursor/rules/*.mdc`)

Cursor's rules system is project-scoped — there is no global equivalent. When
installing project-locally, the installer copies the checked-in Atelier rule set
so Cursor's agent prefers Atelier's MCP tools and starts with `context`:

```markdown
---
description: Atelier-first tool selection for Cursor. Call context first and use Atelier MCP tools before Cursor native tools.
alwaysApply: true
---

Start coding tasks with Atelier `context` using the task, domain, files, and planned tools.
Treat Cursor native tools as fallback-only when an Atelier equivalent exists.
```

---

## Verify

```bash
make verify
```

Or manually:

```bash
atelier-mcp --host cursor --version
```

---

## Expected Behavior

- Cursor connects to the Atelier MCP server via stdio on startup
- Atelier tools (`context`, `trace`, `rescue`, `verify`, `memory`, `read`, `edit`, `sql`, `search`, `compact`, `shell`, `code`) appear in Cursor's tool list
- With `ATELIER_DEV_MODE=1`, all tools are fully visible and active
- `trace` remains the stable observable recording surface
- Cursor's agent uses Atelier's `context` tool for task-level reasoning

---

## Troubleshooting

| Problem                          | Fix                                                                                |
| -------------------------------- | ---------------------------------------------------------------------------------- |
| "atelier-mcp: command not found" | Run `pip install atelier-runtime` or reinstall via `make install`                  |
| MCP tools not showing up         | Restart Cursor completely (Cmd+Shift+P → "Developer: Reload Window")              |
| Tools fail with "host not cursor" | Check `~/.cursor/mcp.json` has `--host cursor` in args                             |
| Cursor workspace not detected    | For global installs, ensure you open a folder/workspace in Cursor before using MCP |

---

## Uninstall

```bash
atelier uninstall
```

Or manually remove the `atelier` entry from `~/.cursor/mcp.json` and delete
`.cursor/rules/atelier.mdc` if present.
