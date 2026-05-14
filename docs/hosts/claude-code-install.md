# Installing Atelier into Claude Code

**Support level**: Full plugin (skills, agents, hooks, MCP server)

---

## Install Modes

| Mode                          | What it installs                        | When to use                               |
| ----------------------------- | --------------------------------------- | ----------------------------------------- |
| **Marketplace** (recommended) | Full plugin via `claude plugin install` | Normal install — agents + skills + MCP    |
| **Dev** (no install)          | Plugin loaded via `--plugin-dir` flag   | Testing plugin changes without install    |
| **MCP-only** (fallback)       | `.mcp.json` entry only, no plugin       | Claude < 2.1 or when plugin install fails |

---

## Mode 1: Marketplace Install (Recommended)

```bash
make install
```

This registers the local Claude plugin source (`atelier`), installs
`atelier@atelier`, and registers the MCP server in Claude's user scope.
Pass `--workspace /path/to/workspace` to write a project-local `.mcp.json`
instead.

The script is idempotent — safe to run again after updates.

### Verify

```bash
make verify
```

All checks should show `PASS`:

- `claude plugin list` shows `atelier@atelier ✔ enabled`
- Plugin source `atelier` is registered
- Global install: `claude mcp list` shows `atelier`
- Workspace install: `.mcp.json` contains atelier server entry

### Manual steps (print-only mode)

```bash
bash scripts/install_claude.sh --print-only
bash scripts/install_claude.sh --print-only --workspace /path/to/workspace
```

---

## Mode 2: Dev Mode (No Install)

For testing plugin changes without a full install:

```bash
bash scripts/install_claude.sh --print-only
```

This prints the command to run:

```bash
claude --plugin-dir /abs/path/to/integrations/claude/plugin
```

No `claude plugin install` or marketplace registration needed. Changes to skill
files are picked up on restart.

Install profile selection:

- `ATELIER_PROFILE=stable` is the default install profile.
- `ATELIER_PROFILE=dev` stages the dev-oriented plugin artifacts.
- `ATELIER_DEV_MODE=1` is still required for runtime-gated dev tools.

---

## Mode 3: MCP-Only Fallback

```bash
bash scripts/install_claude.sh --print-only
```

> **WARNING**: This is NOT the full plugin. It installs the MCP server entry in
> `.mcp.json` only if you apply the printed manual steps. Agents and `/atelier:*` skills are NOT available.
> Use this only when `claude plugin install` is unavailable.

---

## What Gets Installed (Full Plugin)

| Artifact            | Global install                                        | `--workspace DIR` install                 |
| ------------------- | ----------------------------------------------------- | ----------------------------------------- |
| Claude plugin       | `~/.claude/plugins/cache/...`                         | same plugin install                       |
| Plugin listing      | `~/.claude/plugins/installed_plugins.json`            | same plugin listing                       |
| Marketplace entry   | `~/.claude/settings.json` (known_marketplaces)        | same marketplace entry                    |
| MCP server config   | Claude user MCP scope (`claude mcp add --scope user`) | `<workspace>/.mcp.json`                   |
| Workspace env       | not written                                           | `<workspace>/.claude/settings.local.json` |
| Skills/agents/hooks | bundled in `integrations/claude/plugin/`              | bundled in the same plugin                |

---

## First Task

Start Claude Code in your workspace and type:

```text
/atelier:status
```

You should see the Atelier runtime info (run ledger, service-backed state, version).

## Slash Commands (Skills)

All commands use the `/atelier:name` format (colon, not dash):

| Command                               | Description                                             |
| ------------------------------------- | ------------------------------------------------------- |
| `/atelier:status [session_id]`        | Show current run ledger — plan, facts, blockers, alerts |
| `/atelier:context <domain>`           | Show environment context for a domain                   |
| `/atelier:savings`                    | Report savings metrics                                  |
| `/atelier:benchmark [--apply]`        | Run eval suite (dry-run by default)                     |
| `/atelier:analyze-failures`           | Cluster recurring failures across runs                  |
| `/atelier:evals list\|run\|promote`   | Manage eval cases                                       |
| `/atelier:settings [off\|shadow\|on]` | Show or change smart-tool mode                          |

## Agents

Select from the `/agents` list in Claude Code:

| Agent             | Role                                         |
| ----------------- | -------------------------------------------- |
| `atelier:code`    | Main coding agent — full task loop           |
| `atelier:explore` | Read-only repo exploration                   |
| `atelier:review`  | Verifier — plan checks + rubric gate         |
| `atelier:repair`  | Repair specialist — rescue repeated failures |

## V2 Tools — Memory and Context Savings

With `ATELIER_DEV_MODE=1`, the active Atelier MCP surface for Claude Code includes
`context`, `route`, `rescue`, `trace`, `verify`, `memory`, `read`, `edit`,
`sql`, `search`, `compact`, `shell`, and the `code` helpers.

Without developer mode, `trace` remains the most reliable active surface and
some other tools may still appear as passive compatibility stubs.

Host-native file reads, search, shell, slash commands, and agents remain the raw
control surface when you do not need Atelier-specific context, routing, or trace
behavior.

## Troubleshooting

| Problem                       | Fix                                                                     |
| ----------------------------- | ----------------------------------------------------------------------- |
| Not in `claude plugin list`   | Run `make install`                                                      |
| Plugin listed but not enabled | Run `claude plugin enable atelier@atelier`                              |
| Validation fails              | Run `claude plugin validate integrations/claude/plugin/`                |
| MCP tools missing             | Global: run `claude mcp list`; workspace: check `.mcp.json`             |
| Hooks firing unexpectedly     | Set `"enabled": false` in `integrations/claude/plugin/hooks/hooks.json` |
| Want to test without install  | Use `bash scripts/install_claude.sh --print-only`                       |

## Uninstall

```bash
bash scripts/uninstall_claude.sh
bash scripts/uninstall_claude.sh --workspace /path/to/workspace
```
