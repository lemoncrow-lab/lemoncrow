# Installing LemonCrow into Antigravity

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

Global installs also copy the LemonCrow Antigravity plugin metadata and shared
skills for `agy`, so Antigravity sessions get stronger LemonCrow-first guidance
instead of only a bare MCP registration.

The MCP config registers LemonCrow as a stdio server:

```json
{
  "servers": {
    "lemon": {
      "type": "stdio",
      "command": "lemon mcp",
      "args": ["--host", "antigravity"]
    }
  }
}
```

Global installs also try:

```bash
antigravity --add-mcp '{"name":"lemon","command":"lemon mcp","args":["--host","antigravity"]}'
```

## Verify

```bash
make verify
```

## First Task

Open the workspace in Antigravity or start `agy` in the repo and ask it to use LemonCrow context before coding.

## Expected Behavior

- Antigravity can invoke LemonCrow MCP tools through the local LemonCrow MCP wrapper
- `agy` can work against the same workspace and MCP surface
- Workspace installs stay local to `.vscode/mcp.json`
- Active context/retrieval/verify tools require `LEMONCROW_DEV_MODE=1`; otherwise some tools may be visible but return passive `noop`

## Troubleshooting

| Problem                 | Fix                                                                       |
| ----------------------- | ------------------------------------------------------------------------- |
| MCP tools not loading   | Reload Antigravity; check user `mcp.json` or workspace `.vscode/mcp.json` |
| `antigravity` not found | Install the Antigravity app or ensure it is on `PATH`                     |
| `agy` not found         | Install/configure the companion CLI if you want terminal-first usage      |

## Uninstall

```bash
bash scripts/uninstall_antigravity.sh
bash scripts/uninstall_antigravity.sh --workspace /path/to/workspace
```
