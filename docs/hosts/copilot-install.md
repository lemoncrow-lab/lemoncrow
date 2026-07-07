# Installing Atelier into Copilot

**Support level**: MCP config + Copilot instructions + agent + tasks

---

## Quick Install

```bash
make install
```

By default this installs VS Code user/global MCP, instructions, and task presets. For project-local Copilot artifacts:

```bash
bash scripts/install_copilot.sh --workspace /path/to/workspace
```

To configure project-specific role models after the global install, run:

```bash
uv run atelier init --configure-models
```

Inside a git workspace, the init wizard writes `<workspace>/.atelier/settings.json`
and refreshes the workspace Copilot agent at
`<workspace>/.github/agents/atelier.code.agent.md`. Those workspace role agents are the
surface Copilot uses for the current project, so it can carry explicit model
pins even when the global install stays neutral.

---

## What Gets Installed

| Artifact             | Global install                                    | `--workspace DIR` install                                                                         |
| -------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| MCP server config    | VS Code user `mcp.json`                           | `<workspace>/.vscode/mcp.json`                                                                    |
| Copilot instructions | `~/.copilot/instructions/atelier.instructions.md` | `<workspace>/.github/copilot-instructions.md`                                                     |
| Agent                | not installed globally                            | `<workspace>/.github/agents/atelier.code.agent.md` plus the other `atelier.<role>.agent.md` files |
| Task presets         | VS Code user `tasks.json` (merged)                | `<workspace>/.vscode/tasks.json` (merged)                                                         |

The MCP config registers Atelier as a stdio server:

```

## Project-local model config

The intended flow is:

1. Install Atelier globally so Copilot can see the MCP server and default instructions.
2. Run `uv run atelier init --configure-models` inside a repository.
3. Let the wizard write `.atelier/settings.json` plus a workspace-local
   `.github/agents/atelier.code.agent.md` and the other `atelier.<role>.agent.md` files.

`"auto"` means omit an explicit model pin for that host surface. Concrete values
write the selected model directly into the workspace agent frontmatter.json
&#123;
  "servers": &#123;
    "atelier": {
      "type": "stdio",
      "command": "atelier mcp",
      "args": ["--host", "copilot"],
      "env": {
        "ATELIER_WORKSPACE_ROOT": "<workspace>"
      }
    }
  &#125;
&#125;
```

## Verify

```bash
make verify
```

## First Task

Open Copilot Chat and either run `Atelier: Copilot Preflight` from the VS Code
task picker or, if developer mode is enabled for the MCP server, ask:

```text
Use Atelier context for this task and record a trace summary when it is done.
```

Additional workspace helpers:

- `Atelier: Session Summary` prints the latest Atelier per-session cost and savings breakdown after a Copilot task or chat run.
- `Atelier: Worktree Bootstrap` writes `.env.worktree` with stable per-worktree ports and container names.
- `Atelier: Runtime Evidence` captures `health`, `analytics/summary`, and `v1/traces` into `reports/runtime-evidence/latest.json`.

## Expected Behavior

- Copilot Chat can invoke Atelier MCP tools through the local Atelier MCP wrapper
- `copilot-instructions.md` provides Atelier context to every Copilot session
- `atelier` agent is available from the agent picker
- The installed Copilot instructions and agent explicitly map native `search/codebase`, `search`, `edit/editFiles`, and `execute/runInTerminal` back to Atelier MCP equivalents
- `Atelier: Copilot Preflight` runs the shell-based preflight before you continue in Copilot Chat
- `Atelier: Worktree Bootstrap` makes local stacks easier to boot from multiple worktrees
- `Atelier: Runtime Evidence` provides a repeatable validation artifact for service behavior
- Active context/retrieval/verify tools require `ATELIER_DEV_MODE=1`; otherwise some tools may be visible but return passive `noop`
- Projection links in the session UI open `/projection?path=...`, where Copilot users can inspect compact vs exact views, token savings, and mapping segments before retrying an edit

## Why Tasks Still Use Shell

Copilot's MCP support applies to chat/tool calls inside the Copilot session. VS Code
`tasks.json` entries are shell tasks, so the preflight task has to spawn the
`atelier` CLI rather than invoke MCP directly. The MCP server remains the primary
integration surface for in-chat `context`, `trace`, `memory`, and related Atelier tools.

## Projection Workflow

When Copilot reads code through Atelier, compact reads can carry `projection_mapping`
metadata. If a projection edit is ambiguous, the failure now returns machine-readable
`retry_with` guidance. Follow that reread instead of guessing a transformed span.

## Reload Required

After install, reload the VS Code window:  
`Ctrl+Shift+P` → `Developer: Reload Window`

## Troubleshooting

| Problem               | Fix                                                                          |
| --------------------- | ---------------------------------------------------------------------------- |
| MCP tools not loading | Reload VS Code window; check user `mcp.json` or workspace `.vscode/mcp.json` |
| `code` CLI not found  | Install VS Code CLI: in VS Code, run "Install 'code' command in PATH"        |

## MCP Tools and Dev Mode

With developer mode enabled (`ATELIER_DEV_MODE=1`), the active Atelier MCP
surface for Copilot includes all stable tools plus dev-only tools such as
`rescue` and `verify`.

Without developer mode, the full stable surface is still active, including
`code`, while dev-only tools remain passive/noop.

VS Code's native search, file reads, and editing remain the preferred raw-access
tools when you do not need Atelier-specific context or trace behavior.

Disable Atelier cache: `ATELIER_CACHE_DISABLED=1`.

## Uninstall

```bash
bash scripts/uninstall_copilot.sh
bash scripts/uninstall_copilot.sh --workspace /path/to/workspace
```
