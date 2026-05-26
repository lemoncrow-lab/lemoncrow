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

The installer merges an `atelier` entry into the `mcp` key:

```json
&#123;
  "mcp": &#123;
    "atelier": &#123;
      "type": "local",
      "command": ["atelier-mcp", "--host", "opencode"],
      "environment": {
        "ATELIER_WORKSPACE_ROOT": "<workspace>"
      }
    &#125;
  &#125;
&#125;
```

## Verify

```bash
make verify
```

## Expected Behavior

- opencode connects to the local Atelier HTTP service via the MCP stdio wrapper
- Workspace Atelier agent profile is installed at `.opencode/agents/atelier.md`
- The installer sets `default_agent` to `atelier` even when the config already exists
- With `ATELIER_DEV_MODE=1`, opencode can actively use `context`, `route`, `rescue`, `verify`, `memory`, `read`, `edit`, `sql`, `search`, `compact`, `shell`, and the `atelier_code_*` helpers
- `trace` remains the stable observable recording surface

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
