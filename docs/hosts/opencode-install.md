# OpenCode integration

LemonCrow integrates with OpenCode as a local plugin/tool integration while OpenCode keeps its own model, provider account, and normal UI.

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
```

For one workspace:

```bash
bash scripts/install_opencode.sh --workspace /path/to/repo
```

The development installer can write:

- the OpenCode tool-connection config;
- a LemonCrow agent profile;
- a small prompt-time guidance plugin;
- workspace-specific LemonCrow settings.

Restart OpenCode after installation or plugin changes.

## Verify

```bash
lc rt status --host opencode
lc rt status
```

From a source checkout:

```bash
make verify
```

## Implementation/debug files

OpenCode stores its external-tool configuration in `opencode.json` under an `mcp` key. That is OpenCode's protocol/config format; users should think of the result as the LemonCrow integration, not as a separate server they need to manage.

Global and workspace paths commonly include:

```text
~/.config/opencode/opencode.json
~/.config/opencode/plugins/lemoncrow-nudge.js
<workspace>/opencode.json
<workspace>/.opencode/plugins/
```

## Troubleshooting

| Problem | Check |
| --- | --- |
| LemonCrow tools do not appear | Restart OpenCode, then run `lc rt status --host opencode` |
| Prompt guidance does not appear | Check the LemonCrow plugin file and restart OpenCode |
| Workspace config is ignored | Confirm you launched OpenCode from the intended workspace |
