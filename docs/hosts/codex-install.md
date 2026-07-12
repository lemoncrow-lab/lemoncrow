# Installing LemonCrow into Codex CLI

**Support level**: Codex plugin with bundled LemonCrow MCP, global code-mode instructions, and standalone custom agents.

## Recommended user flow

Install LemonCrow into Codex globally once from the LemonCrow checkout:

```bash
bash scripts/install_codex.sh
```

Then initialize or refresh each project with the globally installed LemonCrow CLI:

```bash
cd /path/to/project
lc init
```

If project initialization already runs automatically in your workflow, no separate
workspace installer command is needed. Restart Codex after the first global install
or after plugin changes.

## What the global installer writes

| Artifact | Global path | Purpose |
| --- | --- | --- |
| Plugin source | `~/.codex/plugins/lemoncrow/` | Skills, hooks, bundled MCP config, and plugin metadata |
| Marketplace | `~/.agents/plugins/marketplace.json` | Makes `lemoncrow@lemoncrow-local` discoverable by Codex |
| Main-session instructions | `~/.codex/AGENTS.md` | Makes Codex `Main [default]` operate as `lemoncrow:code` |
| Custom agents | `~/.codex/agents/lemoncrow.*.toml` | Seven globally available custom subagents |

The plugin's `.mcp.json` launches:

```text
lc mcp --host codex
```

The installer attempts non-interactive plugin activation. Some Codex builds require
a restart and manual activation through `/plugins`; pending activation is reported as
a warning and does not invalidate the successfully installed instructions or agents.

## What `lc init` syncs into a project

When Codex is installed or the project already contains `.codex/`, `lc init`:

- creates or refreshes the managed LemonCrow block in `<project>/AGENTS.md` using the generic, host-neutral `integrations/AGENTS.lemoncrow.md` guide;
- writes seven standalone files under `<project>/.codex/agents/`;
- applies project-specific model overrides when configured;
- removes obsolete LemonCrow-owned `[agents.lemoncrow_*]` registration tables from `<project>/.codex/config.toml` while preserving unrelated Codex settings.

`lc init` does not need to duplicate the global plugin or add a second MCP
registration. The globally installed plugin provides the Codex-specific tool surface;
the project files provide local instructions and locally refreshed agent definitions.

## Installed agents

The generated standalone files are:

```text
.codex/agents/lemoncrow.code.toml
.codex/agents/lemoncrow.explore.toml
.codex/agents/lemoncrow.execute.toml
.codex/agents/lemoncrow.plan.toml
.codex/agents/lemoncrow.research.toml
.codex/agents/lemoncrow.review.toml
.codex/agents/lemoncrow.solve.toml
```

Each file contains `name`, `description`, optional `model`, and
`developer_instructions`. Shared instruction placeholders are expanded before the file
is written.

Codex keeps the root thread label `Main [default]`; that root session receives the
`lemoncrow:code` behavior from `~/.codex/AGENTS.md`. Custom agents are spawned by name,
for example:

```text
Spawn lemoncrow.explore to map this repository.
```

or:

```text
Spawn lemoncrow.code to implement this change.
```

`/agent` is an active-thread switcher, so a custom agent appears there only after it
has been spawned.

## Verify the global install

```bash
codex --version
ls ~/.codex/agents/lemoncrow.*.toml
grep -n "You are operating as.*lemoncrow:code" ~/.codex/AGENTS.md
python -m json.tool ~/.codex/plugins/lemoncrow/.mcp.json >/dev/null
```

Check plugin activation from inside a restarted Codex session:

```text
/plugins
```

Enable `lemoncrow@lemoncrow-local` if it is staged but not yet active.

## Verify project synchronization

From the project root after `lc init`:

```bash
grep -n "LEMONCROW:CODE START" AGENTS.md
ls .codex/agents/lemoncrow.*.toml
grep -H '^name = ' .codex/agents/lemoncrow.*.toml
! grep -q '^\[agents\.lemoncrow_' .codex/config.toml 2>/dev/null
```

There should be seven TOML files. A missing `.codex/config.toml` is valid when the
project has no other Codex-specific settings.

## Isolated workspace install

For a project that must not use the global installation, the installer still supports:

```bash
bash scripts/install_codex.sh --workspace /path/to/project
```

Do not run both installation modes for the normal global-plus-`lc init` workflow.

## Troubleshooting

| Problem | Check |
| --- | --- |
| Plugin not visible | Restart Codex, open `/plugins`, and enable `lemoncrow@lemoncrow-local`; verify `~/.agents/plugins/marketplace.json` and `~/.codex/plugins/lemoncrow/` exist |
| Main does not behave as LemonCrow code mode | Verify `~/.codex/AGENTS.md` contains `You are operating as *lemoncrow:code*.` and start a new Codex session |
| Agents are missing | Run `lc init` in the repository and verify `.codex/agents/lemoncrow.*.toml` contains seven files |
| `/agent` shows only Main | Spawn a custom agent by name first; `/agent` lists active threads, not every installed definition |
| LemonCrow tools are missing | Confirm the plugin is enabled in `/plugins` and inspect `~/.codex/plugins/lemoncrow/.mcp.json` |
| Old config tables remain | Re-run `lc init`; it removes only LemonCrow-owned legacy `[agents.lemoncrow_*]` sections |

## Uninstall

```bash
bash scripts/uninstall_codex.sh
```

For an isolated workspace installation:

```bash
bash scripts/uninstall_codex.sh --workspace /path/to/project
```
