# Installing Atelier into Gemini CLI

**Support level**: Formal Gemini extension + commands + skills + MCP

---

## Quick Install

```bash
make install
```

By default this links the Gemini extension and enables it for the user. For workspace-only activation:

```bash
bash scripts/install_gemini.sh --workspace /path/to/workspace
```

---

## What Gets Installed

| Artifact                | Global install                                               | `--workspace DIR` install                  |
| ----------------------- | ------------------------------------------------------------ | ------------------------------------------ |
| Extension source        | `integrations/gemini/extension/`                             | same repo extension source                 |
| Extension registration  | `~/.gemini/extensions/atelier` symlink managed by Gemini CLI | same global link managed by Gemini CLI     |
| Activation scope        | user                                                         | workspace only for the requested directory |
| Bundled commands/skills | loaded from the linked extension                             | loaded from the same linked extension      |
| Bundled GEMINI context  | loaded from `extension/GEMINI.md`                            | loaded from the same linked extension      |
| Wrapper launcher        | `~/.local/bin/atelier-gemini`                                | `<workspace>/bin/atelier-gemini`           |

The extension manifest wires Gemini to the local Atelier HTTP service by default:

```json
{
  "name": "atelier",
  "mcpServers": {
    "atelier": {
      "command": "atelier-mcp",
      "cwd": "${workspacePath}",
      "env": {
        "ATELIER_WORKSPACE_ROOT": "${workspacePath}",
        "ATELIER_SERVICE_URL": "http://127.0.0.1:8787"
      }
    }
  },
  "contextFileName": "GEMINI.md"
}
```

> **Note**: The extension expects `atelier-mcp` to be available on `PATH`. The installer uses `gemini extensions link`, so changes under `integrations/gemini/extension/` are picked up after you restart Gemini CLI.

## Verify

```bash
make verify
```

## First Task

Start Gemini CLI and ask:

```
use skill: task
```

## Expected Behavior

- Gemini CLI loads the Atelier extension on startup
- The bundled MCP server talks to the local Atelier HTTP service at `http://127.0.0.1:8787`
- Shared runtime state and memory are owned by that service, not the workspace
- Bundled commands (`/atelier:status`, `/atelier:context`) and bundled skills are installed with the extension
- `atelier-gemini --task "..."` can emit live start-time optimizer guidance before handing off to Gemini CLI

## Troubleshooting

| Problem                                     | Fix                                                                                         |
| ------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Extension not listed                        | Re-run `gemini extensions link integrations/gemini/extension`                               |
| MCP tools missing                           | Restart Gemini CLI and verify `atelier-mcp` is on `PATH`                                    |
| Workspace-only activation not taking effect | Re-run `bash scripts/install_gemini.sh --workspace /path/to/workspace` from the target repo |
| Repo path changed                           | Re-run `make install`                                                                       |

## V2 Tools — Memory, Context Savings, and Lesson Pipeline

All V2 tools are available via the Atelier MCP server. These are **Atelier augmentations** — Gemini CLI native tools remain the primary interface.

| Tool                    | Boundary             | Description                                               |
| ----------------------- | -------------------- | --------------------------------------------------------- |
| `memory`                | Atelier augmentation | Store named value in agent memory                         |
| `memory`                | Atelier augmentation | Retrieve named memory block                               |
| `memory`                | Atelier augmentation | FTS + vector search over archival memory                  |
| `memory`                | Atelier augmentation | Persist text passage to archival memory                   |
| `memory`                | Atelier augmentation | Compact sleeptime memory (reduces context window)         |
| `search`                | Atelier augmentation | Token-saving combined search + read                       |
| `edit`                  | Atelier augmentation | Deterministic multi-file batch edits (optional)           |
| `atelier sql inspect`   | Atelier augmentation | Read-only SQL schema/data inspection                      |
| `compact`               | Atelier augmentation | Advise before context compaction; provides reinject hints |
| `atelier lesson inbox`  | Atelier augmentation | List lesson candidates awaiting decision                  |
| `atelier lesson decide` | Atelier augmentation | Approve or reject a lesson candidate                      |

## Uninstall

```bash
bash scripts/uninstall_gemini.sh
bash scripts/uninstall_gemini.sh --workspace /path/to/workspace
```
