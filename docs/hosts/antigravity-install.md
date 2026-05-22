# Installing Atelier into Antigravity

**Support level**: MCP config for Antigravity plus `agy` companion CLI support

---

## Quick Install

```bash
make install
```

For project-local Antigravity artifacts:

```bash
bash scripts/install_antigravity.sh --workspace /path/to/workspace
```

---

## What Gets Installed

| Artifact          | Global install                            | `--workspace DIR` install      |
| ----------------- | ----------------------------------------- | ------------------------------ |
| MCP server config | `~/.config/Antigravity/User/mcp.json`     | `<workspace>/.vscode/mcp.json` |
| Host instructions | Existing workspace `AGENTS.md` entrypoint | same workspace entrypoint      |

The MCP config registers Atelier as a stdio server:

```json
{
  "servers": {
    "atelier": {
      "type": "stdio",
      "command": "atelier-mcp",
      "args": ["--host", "antigravity"]
    }
  }
}
```

Global installs also try:

```bash
antigravity --add-mcp '{"name":"atelier","command":"atelier-mcp","args":["--host","antigravity"]}'
```

## Verify

```bash
make verify
```

## First Task

Open the workspace in Antigravity or start `agy` in the repo and ask it to use Atelier context before coding.

## Expected Behavior

- Antigravity can invoke Atelier MCP tools through the local Atelier MCP wrapper
- `agy` can work against the same workspace and MCP surface
- Workspace installs stay local to `.vscode/mcp.json`
- Active context/retrieval/verify tools require `ATELIER_DEV_MODE=1`; otherwise some tools may be visible but return passive `noop`

## Troubleshooting

| Problem                  | Fix                                                                     |
| ------------------------ | ----------------------------------------------------------------------- |
| MCP tools not loading    | Reload Antigravity; check user `mcp.json` or workspace `.vscode/mcp.json` |
| `antigravity` not found  | Install the Antigravity app or ensure it is on `PATH`                   |
| `agy` not found          | Install/configure the companion CLI if you want terminal-first usage     |

## Uninstall

```bash
bash scripts/uninstall_antigravity.sh
bash scripts/uninstall_antigravity.sh --workspace /path/to/workspace
```
