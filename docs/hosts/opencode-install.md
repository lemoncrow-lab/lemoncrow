# Installing LemonCrow into opencode

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
bash scripts/run_opencode_with_lemoncrow.sh --workspace /path/to/workspace
```

---

## What Gets Installed

| Artifact          | Global install                                | `--workspace DIR` install                        |
| ----------------- | --------------------------------------------- | ------------------------------------------------ |
| MCP server config | `~/.config/opencode/opencode.json`            | `<workspace>/opencode.json`                      |
| Agent profile     | `~/.config/opencode/agents/code.md`           | `<workspace>/.opencode/agents/lemoncrow.code.md`   |
| Nudge plugin      | `~/.config/opencode/plugins/lemoncrow-nudge.js` | `<workspace>/.opencode/plugins/lemoncrow-nudge.js` |

The installer merges:

1. `mcp.lemoncrow` for `lemon mcp`
2. `provider.lemoncrow` for OpenAI-compatible chat completions (`http://127.0.0.1:8787/v1`)
3. A local `chat.message` plugin that injects LemonCrow guidance before a user prompt is sent

MCP entry:

```json
&#123;
  "mcp": &#123;
    "lemon": &#123;
      "type": "local",
      "command": ["lemon mcp", "--host", "opencode"],
      "environment": {
        "LEMONCROW_WORKSPACE_ROOT": "<workspace>"
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
bash scripts/run_opencode_with_lemoncrow.sh --dry-run --workspace /path/to/workspace
```

## Expected Behavior

- opencode connects to the local LemonCrow HTTP service via the MCP stdio wrapper
- Workspace LemonCrow agent profile is installed at `.opencode/agents/lemoncrow.code.md`
- The installer sets `default_agent` to `code` even when the config already exists
- The local plugin adds context-window and multi-file-edit nudges to submitted prompts when applicable
- opencode loads local plugins at startup; restart it after installation or plugin changes
- opencode does not expose a Codex-style `/hooks` status screen
- With `LEMONCROW_DEV_MODE=1`, opencode can actively use `context`, `route`, `rescue`, `verify`, `memory`, `read`, `edit`, `sql`, `search`, `compact`, `bash`, and the `lemoncrow_code_*` helpers
- `trace` remains the stable observable recording surface

## Troubleshooting

| Problem                  | Fix                                                                                              |
| ------------------------ | ------------------------------------------------------------------------------------------------ |
| MCP tools not showing    | Restart opencode after install                                                                   |
| Prompt nudge not showing | Restart opencode and check `~/.config/opencode/plugins/lemoncrow-nudge.js` or `.opencode/plugins/` |
| Config not found         | Global: check `~/.config/opencode/opencode.json`; workspace: check `opencode.json`               |

## MCP Tools and Dev Mode

With `LEMONCROW_DEV_MODE=1`, the active LemonCrow MCP surface for opencode includes
`context`, `route`, `rescue`, `trace`, `verify`, `memory`, `read`, `edit`,
`sql`, `search`, `compact`, `bash`, and the `code` helpers.

Without developer mode, `trace` remains the most reliable active surface and
some other tools may still appear as passive compatibility stubs.

## Uninstall

```bash
bash scripts/uninstall_opencode.sh
bash scripts/uninstall_opencode.sh --workspace /path/to/workspace
```
