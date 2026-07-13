# Installing LemonCrow into Claude Code

**Support level**: Full plugin (skills, agents, hooks, MCP server)

---

## Install Modes

| Mode                          | What it installs                        | When to use                               |
| ----------------------------- | --------------------------------------- | ----------------------------------------- |
| **Marketplace** (recommended) | Full plugin via `claude plugin install` | Normal install — agents + skills + workflows + MCP    |
| **Dev** (no install)          | Plugin loaded via `--plugin-dir` flag   | Testing plugin changes without install    |
| **MCP-only** (fallback)       | `.mcp.json` entry only, no plugin       | Claude < 2.1.154 or when plugin install fails |

---

## Mode 1: Marketplace Install (Recommended)

```bash
make install
```

This registers the local Claude plugin source (`lc`), installs
`lemoncrow@lemoncrow`, and registers an always-loaded `lc` MCP server in Claude's
user scope. Its tools use the short `mcp__lc__*` namespace and are available
before turn 1. Pass `--workspace /path/to/workspace` to write the same setting in
a project-local `.mcp.json` instead.

The script is idempotent — safe to run again after updates.

To configure project-specific role models after the global install, run:

```bash
uv run lemoncrow init --configure-models
```

The init wizard writes `<workspace>/.lemoncrow/settings.json` and materializes
workspace-local Claude overrides under `.claude/`, so the shared global plugin
can stay neutral while the current project carries explicit per-role models.

### Verify

```bash
make verify
```

All checks should show `PASS`:

- `claude plugin list` shows `lemoncrow@lemoncrow ✔ enabled`
- Plugin source `lc` is registered
- Global install: `claude mcp list` shows `lc`
- Workspace install: `.mcp.json` contains lc server entry

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

- `LEMONCROW_PROFILE=stable` is the default install profile.
- `LEMONCROW_PROFILE=dev` stages the dev-oriented plugin artifacts.
- `LEMONCROW_DEV_MODE=1` is still required for runtime-gated dev tools.

---

## Mode 3: MCP-Only Fallback

```bash
bash scripts/install_claude.sh --print-only
```

> **WARNING**: This is NOT the full plugin. It installs the MCP server entry in
> `.mcp.json` only if you apply the printed manual steps. Agents and `/lc:*` skills are NOT available.
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
| Skills/agents/hooks/workflows | bundled in `integrations/claude/plugin/`     | bundled in the same plugin                |

---

## Project-local model config

The intended flow is:

1. Install the shared Claude plugin globally.
2. Run `uv run lemoncrow init --configure-models` inside a repository.
3. Let the wizard write `.lemoncrow/settings.json`, `.claude/settings.local.json`,
   `.claude/agents/`, and `.claude/skills/`.

`"auto"` means omit an explicit model pin for the workspace Claude surface.
Concrete values write `model:` into the workspace-local Claude agent files while
the shared global plugin remains unpinned.

## First Task

Start Claude Code in your workspace and type:

```text
/lemoncrow:explore
```

You should switch into the read-only LemonCrow explore mode and get a focused repo-mapping workflow.

## Slash Commands (Skills)

All commands use the `/lemoncrow:name` format (colon, not dash):

| Command             | Description                                    |
| ------------------- | ---------------------------------------------- |
| `/lemoncrow:code`     | Switch to main coding mode                     |
| `/lemoncrow:explore`  | Switch to read-only exploration mode           |
| `/lemoncrow:plan`     | Switch to implementation planning mode         |
| `/lemoncrow:execute` | Switch to focused execution mode               |
| `/lemoncrow:review`   | Switch to adversarial review mode              |
| `/lemoncrow:research` | Switch to external research mode with citations |
| `/lemoncrow:solve`   | Switch to autonomous solve mode                |

## Agents

Select from the `/agents` list in Claude Code:

| Agent             | Role                                         |
| ----------------- | -------------------------------------------- |
| `lemoncrow:code`    | Main coding agent — full task loop           |
| `lemoncrow:explore` | Read-only repo exploration                   |
| `lemoncrow:plan`    | Planner — grounded implementation plan       |
| `lemoncrow:execute` | Executor — focused edits + self-check       |
| `lemoncrow:review`  | Verifier — plan checks + rubric gate         |
| `lemoncrow:research`| External research with citations             |
| `lemoncrow:solve`  | Autonomous solver — artifact/check loop      |

## Dynamic Workflows

Dynamic workflows require **Claude Code v2.1.154 or later** and are still in
research preview.

The LemonCrow Claude plugin now bundles workflow scripts under
`integrations/claude/plugin/workflows/`. The first packaged workflow is
`code-audit.js`, a reusable multi-lens audit that fans out security,
performance, and test-review passes before consolidating the findings into one
report. It also now bundles `gate-benchmark.js`, which drives the repo's live
benchmark/reporting surfaces and returns a strict `PASS` / `FAIL` /
`INSUFFICIENT_DATA` gate verdict instead of relying on deleted A/B infra.

Use `/workflows` in Claude Code to inspect available workflow runs and saved
scripts. Keep long-running workflow permissions broad enough for read/search
operations, or the run may pause on approval prompts. `bash
scripts/verify_claude.sh` also checks that the packaged `workflows/` surface,
`code-audit.js`, and the workflow README are present before you rely on runtime
discovery. The packaged workflow fixture
`workflows/fixtures/code-audit-review-fixture.json` measures the intended
adversarial cross-check win over a single-pass review so the pattern stays
regression-tested even while Claude's workflow runtime remains a preview
surface.

## V2 Tools — Memory and Context Savings

With `LEMONCROW_DEV_MODE=1`, the active LemonCrow MCP surface for Claude Code includes
`context`, `route`, `rescue`, `record`, `verify`, `memory`, `read`, `edit`,
`sql`, `search`, `compact`, `bash`, and the `code` helpers.

Without developer mode, `record` remains the most reliable active surface and
some other tools may still appear as passive compatibility stubs.

Host-native file reads, search, shell, slash commands, and agents remain the raw
control surface when you do not need LemonCrow-specific context, routing, or trace
behavior.

## Troubleshooting

| Problem                       | Fix                                                                     |
| ----------------------------- | ----------------------------------------------------------------------- |
| Not in `claude plugin list`   | Run `make install`                                                      |
| Plugin listed but not enabled | Run `claude plugin enable lemoncrow@lemoncrow`                              |
| Validation fails              | Run `claude plugin validate integrations/claude/plugin/`                |
| MCP tools missing             | Global: run `claude mcp list`; workspace: check `.mcp.json`             |
| Hooks firing unexpectedly     | Set `"enabled": false` in `integrations/claude/plugin/hooks/hooks.json` |
| Want to test without install  | Use `bash scripts/install_claude.sh --print-only`                       |

## Uninstall

```bash
bash scripts/uninstall_claude.sh
bash scripts/uninstall_claude.sh --workspace /path/to/workspace
```
