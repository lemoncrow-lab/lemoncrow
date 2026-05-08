# Installing Atelier into opencode

**Support level**: MCP + workspace agent profile

---

## Quick Install

```bash
make install
```

By default this installs opencode user/global config. For a project-local install:

```bash
bash scripts/install_opencode.sh --workspace /path/to/workspace
```

---

## What Gets Installed

| Artifact          | Global install                         | `--workspace DIR` install                 |
| ----------------- | -------------------------------------- | ----------------------------------------- |
| MCP server config | `~/.config/opencode/opencode.json`     | `<workspace>/opencode.json`               |
| Agent profile     | `~/.config/opencode/agents/atelier.md` | `<workspace>/.opencode/agents/atelier.md` |

The installer merges an `atelier` entry into the `mcp` key:

```json
&#123;
  "mcp": &#123;
    "atelier": &#123;
      "type": "local",
      "command": ["<atelier_repo>/scripts/atelier_mcp_stdio.sh"],
      "environment": &#123;
        "ATELIER_WORKSPACE_ROOT": "<workspace>",
        "ATELIER_ROOT": "~/.atelier"
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

Start opencode in your workspace and ask:

```
use atelier to check this plan
```

## Expected Behavior

- opencode connects to Atelier via MCP stdio
- Workspace Atelier agent profile is installed at `.opencode/agents/atelier.md`
- Canonical MCP tools (`lint`, `reasoning`, etc.) are available
- Compatibility aliases (`lint`, `atelier_status`, etc.) are also available

## Troubleshooting

| Problem               | Fix                                                                                |
| --------------------- | ---------------------------------------------------------------------------------- |
| MCP tools not showing | Restart opencode after install                                                     |
| Config not found      | Global: check `~/.config/opencode/opencode.json`; workspace: check `opencode.json` |

## V2 Tools — Memory, Context Savings, and Lesson Pipeline

All V2 tools are available via the Atelier MCP server. These are **Atelier augmentations** — opencode native tools remain the primary interface.

| Tool                    | Boundary             | Description                                               |
| ----------------------- | -------------------- | --------------------------------------------------------- |
| `memory`                | Atelier augmentation | Store named value in agent memory                         |
| `memory`                | Atelier augmentation | Retrieve named memory block                               |
| `memory`                | Atelier augmentation | FTS + vector search over archival memory                  |
| `memory`                | Atelier augmentation | Persist text passage to archival memory                   |
| `memory`                | Atelier augmentation | Compact sleeptime memory (reduces context window)         |
| `search`                | Atelier augmentation | Token-saving combined search + read                       |
| `edit`                  | Atelier augmentation | Deterministic multi-file batch edits (optional)           |
| `atelier bench runtime` | Atelier augmentation | Capability efficiency metrics                             |
| `compact`               | Atelier augmentation | Advise before context compaction; provides reinject hints |
| `atelier lesson inbox`  | Atelier augmentation | List lesson candidates awaiting decision                  |
| `atelier lesson decide` | Atelier augmentation | Approve or reject a lesson candidate                      |

## Uninstall

```bash
bash scripts/uninstall_opencode.sh
bash scripts/uninstall_opencode.sh --workspace /path/to/workspace
```
