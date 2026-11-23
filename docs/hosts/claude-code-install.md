# Claude Code integration

LemonCrow integrates with Claude Code as a plugin. You can try it for one launch without changing saved Claude configuration, or keep the plugin enabled persistently.


## Installation modes

LemonCrow supports three Claude Code installation modes:

1. **Marketplace/plugin install (normal):** `lc rt enable claude` registers the local LemonCrow marketplace source and installs `lemoncrow@lemoncrow`.
2. **Dev/source checkout:** run Claude directly with `claude --plugin-dir /path/to/lemoncrow/integrations/claude/plugin` when developing the plugin without changing the installed marketplace copy.
3. **MCP-only fallback:** the LemonCrow tool connection can still be registered without relying on plugin-only features. This keeps the core Workspaces tools available, but plugin agents, workflows, Mods, and lifecycle hooks are not all present.

The bundled Claude agents use plugin namespacing. For example, the coding agent is exposed as `/lemoncrow:code` using the colon namespace form.

Dynamic plugin workflows require Claude Code **2.1.154+**. The shipped workflow entry points include `/workflows/code-audit.js` and `/workflows/gate-benchmark.js`; `bash scripts/verify_claude.sh` checks the workflow/version gate. Native Verified Local Work through Claude Mods requires the newer **2.1.287+** Mod capability.

## Try it in the current repository

```bash
lc with claude
```

Run that from a Git worktree. LemonCrow prepares the local repository index and launches your normal Claude Code with the LemonCrow plugin available for that run.

Claude keeps its normal account, model selection, credentials, and saved settings.

## Keep LemonCrow enabled

```bash
lc rt enable claude
```

Check the integration:

```bash
lc rt status --host claude
```

Remove it:

```bash
lc rt disable claude
```

## Source-checkout / plugin development

From a LemonCrow source checkout:

```bash
bash scripts/install_claude.sh
```

For workspace-specific integration artifacts:

```bash
bash scripts/install_claude.sh --workspace /path/to/repo
```

The installer stages LemonCrow's Claude plugin, including skills, agents, hooks, and the tool connection Claude uses to call LemonCrow.

On Claude Code versions with Mods support (2.1.287+), the plugin also uses a measurement-only Mod and an optional native-Bash Verified Local Work adapter. The adapter runs **after** Claude's normal Bash permission/tool execution; it does not auto-approve commands or change Claude's model. If Mods are unavailable, LemonCrow's MCP Bash path still provides Verified Local Work coverage.

## Verify

```bash
bash scripts/verify_claude.sh
```

You can also inspect Claude's own plugin/tool configuration:

```bash
claude plugin list
claude mcp list
```

Those are Claude's native debug/config commands; normal LemonCrow use does not require you to operate a separate server.

## What gets installed

Depending on global vs workspace scope, LemonCrow can install:

- the Claude plugin bundle;
- LemonCrow skills/agents/workflows;
- lifecycle hooks used for evidence and attribution;
- the host tool-connection entry;
- workspace-local LemonCrow settings/agent overrides.

The installer is designed to be repeatable and to preserve unrelated Claude configuration.

## Manual / fallback setup

Use the installer's print-only mode when you need an audited/manual setup:

```bash
bash scripts/install_claude.sh --print-only
bash scripts/install_claude.sh --print-only --workspace /path/to/repo
```

The printed host configuration may include Claude's underlying MCP configuration because that is how Claude exposes external tools. Treat that as an implementation detail of the plugin integration, not a separate LemonCrow product mode.

## Troubleshooting

If `lc with claude` works but a persistent install does not:

1. restart Claude Code after installation;
2. run `lc rt status --host claude`;
3. run `lc rt status`;
4. use `claude plugin list` / `claude mcp list` only for host-level debugging.
