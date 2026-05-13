# Installing Atelier into Copilot

**Support level**: MCP config + Copilot instructions + chat mode + tasks

---

## Quick Install

```bash
make install
```

By default this installs VS Code user/global MCP, instructions, and task presets. For project-local Copilot artifacts:

```bash
bash scripts/install_copilot.sh --workspace /path/to/workspace
```

---

## What Gets Installed

| Artifact             | Global install                                    | `--workspace DIR` install                           |
| -------------------- | ------------------------------------------------- | --------------------------------------------------- |
| MCP server config    | VS Code user `mcp.json`                           | `<workspace>/.vscode/mcp.json`                      |
| Copilot instructions | `~/.copilot/instructions/atelier.instructions.md` | `<workspace>/.github/copilot-instructions.md`       |
| Chat mode            | not installed globally                            | `<workspace>/.github/chatmodes/atelier.chatmode.md` |
| Task presets         | VS Code user `tasks.json` (merged)                | `<workspace>/.vscode/tasks.json` (merged)           |

The MCP config registers Atelier as a stdio server:

```json
&#123;
  "servers": &#123;
    "atelier": &#123;
      "type": "stdio",
      "command": "<atelier_repo>/scripts/atelier_mcp_stdio.sh",
      "args": [],
      "env": &#123;
        "ATELIER_WORKSPACE_ROOT": "<workspace>",
        "ATELIER_SERVICE_URL": "http://127.0.0.1:8787"
      &#125;
    &#125;
  &#125;
&#125;
```

## Verify

```bash
make verify
```

## First Task

Open Copilot Chat and ask:

```
@atelier record task context for this change
```

Or use a prompt like:

```
Use atelier to check the plan for: [your task here]
```

## Expected Behavior

- Copilot Chat can invoke Atelier MCP tools through the local Atelier HTTP service
- `copilot-instructions.md` provides Atelier context to every Copilot session
- `atelier` chat mode is available from the chat mode selector
- `Atelier: Copilot Preflight` runs the task-context preflight before you continue in Copilot Chat

## Why Tasks Still Use Shell

Copilot's MCP support applies to chat/tool calls inside the Copilot session. VS Code
`tasks.json` entries are shell tasks, so the preflight task has to spawn the
`atelier` CLI rather than invoke MCP directly. The MCP server remains the primary
integration surface for in-chat `task`, `trace`, `memory`, and other Atelier tools.

## Reload Required

After install, reload the VS Code window:  
`Ctrl+Shift+P` → `Developer: Reload Window`

## Troubleshooting

| Problem               | Fix                                                                          |
| --------------------- | ---------------------------------------------------------------------------- |
| MCP tools not loading | Reload VS Code window; check user `mcp.json` or workspace `.vscode/mcp.json` |
| `code` CLI not found  | Install VS Code CLI: in VS Code, run "Install 'code' command in PATH"        |

## V2 Tools — Memory, Context Savings, and Lesson Pipeline

All V2 tools are available via the Atelier MCP server. These are **Atelier augmentations** — VS Code native search, file reads, and editing remain the primary interfaces.

| Tool                    | Boundary             | Description                                               |
| ----------------------- | -------------------- | --------------------------------------------------------- |
| `memory`                | Atelier augmentation | Store named value in agent memory                         |
| `memory`                | Atelier augmentation | Retrieve named memory block                               |
| `memory`                | Atelier augmentation | FTS + vector search over archival memory                  |
| `memory`                | Atelier augmentation | Persist text passage to archival memory                   |
| `memory`                | Atelier augmentation | Compact sleeptime memory (reduces context window)         |
| `search`                | Atelier augmentation | Token-saving combined search + read                       |
| `edit`                  | Atelier augmentation | Deterministic multi-file batch edits (optional)           |
| `atelier benchmark runtime` | Atelier augmentation | Capability efficiency metrics                             |
| `compact`               | Atelier augmentation | Advise before context compaction; provides reinject hints |
| `atelier lesson inbox`  | Atelier augmentation | List lesson candidates awaiting decision                  |
| `atelier lesson decide` | Atelier augmentation | Approve or reject a lesson candidate                      |

Disable Atelier cache: `ATELIER_CACHE_DISABLED=1`.

## Uninstall

```bash
bash scripts/uninstall_copilot.sh
bash scripts/uninstall_copilot.sh --workspace /path/to/workspace
```
