# OpenCode integration

LemonCrow integrates with **OpenCode V1 and V2** while OpenCode keeps its own model, provider account, and normal UI. V2 changes the plugin and MCP configuration API; the installer selects a compatible plugin by `opencode --version` without rewriting unrelated model/provider settings.

V2 uses `mcp.servers.lc` and `permissions` in `opencode.json`, a native `setup(ctx)` plugin, and the `@opencode/plugin/tui` sidebar registered in global `cli.json`. V1 keeps its legacy adapter. Manual V2 settings are in `integrations/opencode/opencode.lemoncrow-v2.template.json` (replace `<WORKSPACE_ROOT>`). The installed OpenCode version (not the presence of a config file) chooses the variant.

## Try it in the current repository

```bash
lc with opencode
```

## Keep LemonCrow enabled

```bash
lc rt enable opencode
```

Check or remove the integration:

```bash
lc rt status --host opencode
lc rt disable opencode
```

## Source-checkout install

```bash
bash scripts/install_opencode.sh
# To inspect the planned variant without touching disk:
bash scripts/install_opencode.sh --dry-run
# For an alternate OpenCode binary (including early opencode2 betas):
OPENCODE_BIN=opencode2 bash scripts/install_opencode.sh
```

For one workspace:

```bash
bash scripts/install_opencode.sh --workspace /path/to/repo
```

The development installer can write:

- the OpenCode tool-connection config;
- a LemonCrow agent profile;
- a version-specific prompt guidance plugin (OpenCode 2 uses session/tool hooks);
- a V2 CLI sidebar showing LemonCrow's recorded usage when a snapshot is available (global install only; OpenCode V2 has no project-local CLI settings);
- workspace-specific LemonCrow settings.

Restart OpenCode after installation or plugin changes.

## Verify

```bash
lc rt status --host opencode
lc rt status
```

From a source checkout:

```bash
make verify-full
# Also test the native MCP handshake and required tool names:
bash scripts/verify_opencode.sh
```

## Implementation/debug files

OpenCode stores its external-tool configuration in `opencode.json` under an `mcp` key. That is OpenCode's protocol/config format; users should think of the result as the LemonCrow integration, not as a separate server they need to manage.

Global and workspace paths commonly include:

```text
~/.config/opencode/opencode.json
~/.config/opencode/plugins/lemoncrow-nudge.js
<workspace>/opencode.json
<workspace>/.opencode/plugins/lemoncrow-nudge-v2.js # OpenCode V2 only
~/.config/opencode/cli.json                    # V2 terminal plugin list
~/.config/opencode/lemoncrow-status-v2/         # V2 sidebar plugin
```

## Troubleshooting

| Problem | Check |
| --- | --- |
| LemonCrow tools do not appear | Restart OpenCode, then run `lc rt status --host opencode` |
| Prompt guidance does not appear | Check the LemonCrow plugin file and restart OpenCode |
| Workspace config is ignored | Confirm you launched OpenCode from the intended workspace |

## OpenCode 2 compatibility notes

- Supported OpenCode 2 plugin hooks: `session.hook("prompt")`, `session.hook("context")` for output token limits and queued nudges, `tool.hook("execute.after")`, and the subscribed `session.idle` event. Verify-before-done calls the documented `ctx.session.prompt` API; errors leave advice queued instead of corrupting the host session.
- V2 CLI integration registers a `sidebar.content` slot. It shows the exported LemonCrow usage snapshot when `LEMONCROW_STATUS_FILE` is present, and a nonblocking placeholder otherwise. The sidebar does not invent savings when no measurement is available.
- Session recall imports both V1 `session/message/part` and V2 `session_v2/session_message` SQLite families when present; it reads but never migrates or alters OpenCode's database. Some V2 event-projection shapes may need additional adapters as the upstream format evolves.
- The `lc with opencode` one-shot launcher uses V2's native `mcp.servers` overlay when it detects OpenCode major version 2. It does not persistently install the sidebar or prompt plugin. Use `scripts/install_opencode.sh` for the full persistent integration.
- **OpenCode is not installed in the CI/source environment by default:** unit tests use a fake CLI and synthetic V2 sessions. Before claiming full upstream qualification, run the installer, `bash scripts/verify_opencode.sh`, and the plugin/CLI smoke checklist against a real installed V2 binary. No LemonCrow server needs to be started.
- To uninstall the workspace bridge, use `bash scripts/uninstall_opencode.sh --workspace /path/to/repo`; uninstall the global bridge separately to remove the global V2 status panel registration.
