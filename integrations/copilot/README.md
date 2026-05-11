# Copilot Integration

Support level: **MCP + custom instructions** — MCP server registration and Copilot instructions.

## What gets installed

| Component           | Location after install                                                                           | Description                             |
| ------------------- | ------------------------------------------------------------------------------------------------ | --------------------------------------- |
| MCP server          | VS Code user `mcp.json` or workspace `.vscode/mcp.json`                                          | Wired to `scripts/atelier_mcp_stdio.sh` |
| Custom instructions | `~/.copilot/instructions/atelier.instructions.md` or workspace `.github/copilot-instructions.md` | Atelier usage instructions              |

The installer writes user/global VS Code config by default. Pass
`--workspace DIR` to create or merge workspace `.vscode/mcp.json` and
`.github/copilot-instructions.md`.

## Install

```bash
make install
```

For manual install (print exact steps):

```bash
bash scripts/install_copilot.sh --print-only
```

## Verify

```bash
make verify
```

## Source

Config source: `atelier/copilot/`
Full guide: `docs/hosts/copilot-install.md`
