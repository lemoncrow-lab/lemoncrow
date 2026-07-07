# Installing Atelier into opencode

**Support level**: MCP + OpenAI-compatible provider + workspace agent profile + prompt-time nudge plugin

---

## Quick Install

```bash
make install
```

By default this installs opencode user/global config. For a project-local install:

```bash
bash scripts/install_opencode.sh --workspace /path/to/workspace
```

One-command run (auto-start service, install config, launch opencode):

```bash
bash scripts/run_opencode_with_atelier.sh --workspace /path/to/workspace
```

---

## What Gets Installed

| Artifact          | Global install                                | `--workspace DIR` install                        |
| ----------------- | --------------------------------------------- | ------------------------------------------------ |
| MCP server config | `~/.config/opencode/opencode.json`            | `<workspace>/opencode.json`                      |
| Agent profile     | `~/.config/opencode/agents/atelier.md`        | `<workspace>/.opencode/agents/atelier.md`        |
| Nudge plugin      | `~/.config/opencode/plugins/atelier-nudge.js` | `<workspace>/.opencode/plugins/atelier-nudge.js` |

The installer merges:

1. `mcp.atelier` for `atelier mcp`
2. `provider.atelier` for OpenAI-compatible chat completions (`http://127.0.0.1:8787/v1`)
3. `model: "atelier/atelier-default"`
4. A local `chat.message` plugin that injects Atelier guidance before a user prompt is sent

MCP entry:

```json
&#123;
  "mcp": &#123;
    "atelier": &#123;
      "type": "local",
      "command": ["atelier mcp", "--host", "opencode"],
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

Manual smoke command:

```bash
bash scripts/run_opencode_with_atelier.sh --dry-run --workspace /path/to/workspace
```

## Expected Behavior

- opencode connects to the local Atelier HTTP service via the MCP stdio wrapper
- Workspace Atelier agent profile is installed at `.opencode/agents/atelier.md`
- The installer sets `default_agent` to `atelier` even when the config already exists
- The local plugin adds context-window and multi-file-edit nudges to submitted prompts when applicable
- opencode loads local plugins at startup; restart it after installation or plugin changes
- opencode does not expose a Codex-style `/hooks` status screen
- With `ATELIER_DEV_MODE=1`, opencode can actively use `context`, `route`, `rescue`, `verify`, `memory`, `read`, `edit`, `sql`, `search`, `compact`, `bash`, and the `atelier_code_*` helpers
- `trace` remains the stable observable recording surface

## Troubleshooting

| Problem                  | Fix                                                                                              |
| ------------------------ | ------------------------------------------------------------------------------------------------ |
| MCP tools not showing    | Restart opencode after install                                                                   |
| Prompt nudge not showing | Restart opencode and check `~/.config/opencode/plugins/atelier-nudge.js` or `.opencode/plugins/` |
| Config not found         | Global: check `~/.config/opencode/opencode.json`; workspace: check `opencode.json`               |

## MCP Tools and Dev Mode

With `ATELIER_DEV_MODE=1`, the active Atelier MCP surface for opencode includes
`context`, `route`, `rescue`, `trace`, `verify`, `memory`, `read`, `edit`,
`sql`, `search`, `compact`, `bash`, and the `code` helpers.

Without developer mode, `trace` remains the most reliable active surface and
some other tools may still appear as passive compatibility stubs.

## Uninstall

```bash
bash scripts/uninstall_opencode.sh
bash scripts/uninstall_opencode.sh --workspace /path/to/workspace
```
