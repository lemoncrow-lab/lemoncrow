# Copilot integration

LemonCrow integrates with GitHub Copilot through VS Code/Copilot instructions, agent/task artifacts, and the host's external-tool connection.

Copilot is an additional supported integration, but the primary Workspaces quick-start path is Claude Code, Codex, OpenCode, or Cursor.

## Source-checkout install

```bash
bash scripts/install_copilot.sh
```

For one workspace:

```bash
bash scripts/install_copilot.sh --workspace /path/to/repo
```

The installer can write:

- Copilot instructions;
- LemonCrow workspace agents;
- VS Code task presets;
- the external-tool connection Copilot uses to call LemonCrow.

## Verify

```bash
make verify-full
lc rt status
```

Reload the VS Code window after installation or integration changes.

## First task

Open Copilot Chat and ask it to use the LemonCrow tools/context for the task. Workspace instructions and the LemonCrow agent guide the host toward the LemonCrow tool surface where supported.

## Evidence and limits

Copilot exposes less lifecycle information than Claude Code or Codex. LemonCrow can still record tool/evidence data when the host exposes it, but some attribution and runtime controls may be more limited.

VS Code tasks are shell tasks, so task presets invoke the `lc` CLI directly rather than behaving like in-chat tools.

## Implementation/debug details

VS Code stores the LemonCrow external-tool connection in an `mcp.json` file. Current installer entries invoke the Workspaces protocol adapter under `lc runtime mcp --host copilot`.

That is a host implementation detail. Users should think of the result as the LemonCrow Copilot integration rather than a separate server they need to manage.
