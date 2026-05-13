# opencode Integration

Atelier integrates with opencode through MCP config plus an Atelier agent profile. Installs are global by default; pass `--workspace DIR` for project-local config.

## Setup

```bash
cd atelier
uv sync --all-extras
make install
make verify
```

## Installed Artifacts

- Global: `~/.config/opencode/opencode.json` with merged `mcp.atelier`
- Global: `~/.config/opencode/agents/atelier.md`
- Workspace: `<workspace>/opencode.json` and `<workspace>/.opencode/agents/atelier.md` when `--workspace DIR` is used

## MCP Config Shape

```json
&#123;
  "default_agent": "atelier",
  "mcp": &#123;
    "atelier": &#123;
      "type": "local",
      "command": ["<atelier_repo>/scripts/atelier_mcp_stdio.sh"],
      "environment": &#123;
        "ATELIER_WORKSPACE_ROOT": "<workspace>",
        "ATELIER_SERVICE_URL": "http://127.0.0.1:8787"
      &#125;
    &#125;
  &#125;
&#125;
```

## MCP Tools

Canonical MCP names:

- `task`, `route`, `rescue`, `trace`, `verify`
- `memory`, `search`, `read`, `edit`, `compact`, `atelier_repo_map`

CLI-only workflows include `atelier sql inspect`, `atelier lesson inbox`, `atelier consolidation inbox`, `atelier report`, `atelier proof show`, and `atelier route contract`.
