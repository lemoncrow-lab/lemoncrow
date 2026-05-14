# Installing Atelier into opencode

**Support level**: MCP + workspace agent profile

---

## Quick Install

```bash
make install
```

By default this installs opencode user/global config. For a project-local install:

```bash
bash scripts/install_opencode.sh --workspace /path/to/workspace
```

---

## What Gets Installed

| Artifact          | Global install                         | `--workspace DIR` install                 |
| ----------------- | -------------------------------------- | ----------------------------------------- |
| MCP server config | `~/.config/opencode/opencode.json`     | `<workspace>/opencode.json`               |
| Agent profile     | `~/.config/opencode/agents/atelier.md` | `<workspace>/.opencode/agents/atelier.md` |
| Wrapper launcher  | `~/.local/bin/atelier-opencode`        | `<workspace>/bin/atelier-opencode`        |

The installer merges an `atelier` entry into the `mcp` key:

```json
&#123;
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

## Verify

```bash
make verify
```

## First Task

Start opencode in your workspace and use the installed wrapper:

```bash
atelier-opencode --task "Fix live state drift" --domain state.change
```

## Expected Behavior

- opencode connects to the local Atelier HTTP service via the MCP stdio wrapper
- Workspace Atelier agent profile is installed at `.opencode/agents/atelier.md`
- With `ATELIER_DEV_MODE=1`, opencode can actively use `context`, `route`, `rescue`, `verify`, `memory`, `read`, `edit`, `sql`, `search`, `compact`, `shell`, and the `atelier_code_*` helpers
- `trace` remains the stable observable recording surface
- `atelier-opencode --task "..."` can emit live start-time optimizer guidance before handing off to opencode

## Troubleshooting

| Problem               | Fix                                                                                |
| --------------------- | ---------------------------------------------------------------------------------- |
| MCP tools not showing | Restart opencode after install                                                     |
| Config not found      | Global: check `~/.config/opencode/opencode.json`; workspace: check `opencode.json` |

## MCP Tools and Dev Mode

With `ATELIER_DEV_MODE=1`, the active Atelier MCP surface for opencode includes
`context`, `route`, `rescue`, `trace`, `verify`, `memory`, `read`, `edit`,
`sql`, `search`, `compact`, `shell`, and the `code` helpers.

Without developer mode, `trace` remains the most reliable active surface and
some other tools may still appear as passive compatibility stubs.

## Uninstall

```bash
bash scripts/uninstall_opencode.sh
bash scripts/uninstall_opencode.sh --workspace /path/to/workspace
```
