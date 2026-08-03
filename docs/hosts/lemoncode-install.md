# Installing LemonCrow into LemonCode

**Support level**: Controlled fork — MCP + gateway-native routing + workspace agent profile + built-in sidebar panel

LemonCode is LemonCrow's own fork of opencode
([`lemoncrow-lab/lemoncode`](https://github.com/lemoncrow-lab/lemoncode), vendored
as the `opencode/` submodule). Because the project controls the frontend, it can
apply optimizations that are impossible in a third-party host: prompt and tool
stripping before the gateway call, a built-in LemonCrow telemetry panel, and
gateway-native routing.

---

## Quick Install

```bash
lc code host install
```

This downloads a checksummed release of the host binary into
`<store>/bin/lemoncode-host`. To install the LemonCrow host config as well:

```bash
make install
```

By default this installs LemonCode user/global config. For a project-local install:

```bash
bash scripts/install_lemoncode.sh --workspace /path/to/workspace
```

One-command run (auto-start gateway, launch LemonCode):

```bash
lc code --engine lemoncode
```

`lemoncode` is also a wheel console entry point for the same command, and bare
`lc` with no subcommand dispatches to `lc code`.

---

## What Gets Installed

| Artifact          | Global install                                   | `--workspace DIR` install                          |
| ----------------- | ------------------------------------------------ | -------------------------------------------------- |
| Host binary       | `<store>/bin/lemoncode-host`                     | `<store>/bin/lemoncode-host` (shared per store)    |
| MCP server config | `~/.config/lemoncode/opencode.json`              | `<workspace>/opencode.json`                        |
| Agent profiles    | `~/.config/lemoncode/agents/lemoncrow.<role>.md` | `<workspace>/.opencode/agents/lemoncrow.<role>.md` |
| Nudge plugin      | `~/.config/lemoncode/plugins/lemoncrow-nudge.js` | `<workspace>/.opencode/plugins/lemoncrow-nudge.js` |

The global config directory is `~/.config/lemoncode` and global data lives in
`~/.local/share/lemoncode`, isolated from any stock opencode install. Config
**file** names and the workspace directory are unchanged from opencode:
`opencode.json` and `.opencode/`.

The installer merges:

1. `mcp.lc` for `lc mcp --host lemoncode`
2. `provider.lc` for OpenAI-compatible chat completions (`http://127.0.0.1:8787/v1`)
3. A local `chat.message` plugin that injects LemonCrow guidance before a user prompt is sent

MCP entry:

```json
{
  "mcp": {
    "lc": {
      "type": "local",
      "command": ["lemoncrow", "mcp", "--host", "lemoncode"],
      "environment": {
        "LEMONCROW_WORKSPACE_ROOT": "<workspace>",
        "LEMONCROW_MCP_TOOL_PROFILE": "core"
      }
    }
  }
}
```

## Managing the Host Binary

```bash
lc code host status              # installed version, commit, resolved path, update policy
lc code host install             # download + checksum-verify the latest release
lc code host update              # same, on demand
lc code host build --source opencode  # build from the vendored checkout (requires bun)
lc code host remove              # delete the managed binary
```

The default update policy checks the LemonCode release channel at most once every
six hours. Set `LEMONCODE_HOST_UPDATE=off` to pin the installed build, or
`LEMONCODE_HOST_BIN=/path/to/lemoncode` to point at your own binary.

## Verify

```bash
make verify
```

Manual smoke command:

```bash
bash scripts/install_lemoncode.sh --dry-run --workspace /path/to/workspace
lc code host status --json
```

## Expected Behavior

- LemonCode connects to the local LemonCrow HTTP service via the MCP stdio wrapper
- Workspace LemonCrow agent profiles are installed at `.opencode/agents/lemoncrow.<role>.md`, and `default_agent` is set to `lemoncrow.code`
- `lc code --engine lemoncode` runs the agent loop inside LemonCrow behind a
  token-authenticated loopback gateway; the host itself receives no outer tool calls
- Under `LEMONCODE_MANAGED=1` the fork strips its own system prompt
  (`LEMONCODE_STRIP_HOST_PROMPT`) and tool schemas (`LEMONCODE_STRIP_HOST_TOOLS`)
  before the gateway call, so the host's large prompt is never paid for at the real
  model and host/LemonCrow tools are never executed twice
- While managed, the host's own compaction, auto-update, and model-fetch loops are disabled
- The LemonCrow sidebar panel (tokens in / cached / out, context size, cache efficiency,
  cost, savings, tool + MCP call counts) ships as a builtin — stock opencode needs the
  `lemoncrow-status` plugin for the same view
- Sessions are stored in the same SQLite shape as opencode, under
  `~/.local/share/lemoncode`, so session import and `lc session replay` work identically
- The local plugin adds context-window and multi-file-edit nudges to submitted prompts when applicable
- LemonCode loads local plugins at startup; restart it after installation or plugin changes
- LemonCode does not expose a Codex-style `/hooks` status screen
- With `LEMONCROW_DEV_MODE=1`, LemonCode can actively use `context`, `route`, `rescue`, `verify`, `memory`, `read`, `edit`, `sql`, `search`, `compact`, `bash`, and the `lemoncrow_code_*` helpers
- `trace` remains the stable observable recording surface

## Troubleshooting

| Problem                       | Fix                                                                                                                     |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `lemoncode` host not found    | Run `lc code host install`, or build from the vendored checkout with `lc code host build` (requires `bun`)              |
| Checksum verification failed  | Re-run `lc code host install`; if it persists, build from source instead                                                |
| Host keeps auto-updating      | Set `LEMONCODE_HOST_UPDATE=off`                                                                                         |
| MCP tools not showing         | Restart LemonCode after install                                                                                         |
| Prompt nudge not showing      | Restart LemonCode and check `~/.config/lemoncode/plugins/lemoncrow-nudge.js` or `.opencode/plugins/`                    |
| Config not found              | Global: check `~/.config/lemoncode/opencode.json`; workspace: check `opencode.json`                                     |
| Sidebar panel renders nothing | The panel needs `LEMONCROW_STATUS_FILE`, which `lc code` exports per run; it stays blank until the first turn completes |

## MCP Tools and Dev Mode

With `LEMONCROW_DEV_MODE=1`, the active LemonCrow MCP surface for LemonCode includes
`context`, `route`, `rescue`, `trace`, `verify`, `memory`, `read`, `edit`,
`sql`, `search`, `compact`, `bash`, and the `code` helpers.

Without developer mode, `trace` remains the most reliable active surface and
some other tools may still appear as passive compatibility stubs.

## Uninstall

```bash
bash scripts/uninstall_lemoncode.sh
bash scripts/uninstall_lemoncode.sh --workspace /path/to/workspace
lc code host remove
```
