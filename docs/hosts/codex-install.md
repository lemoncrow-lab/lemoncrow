# Installing Atelier into Codex CLI

**Support level**: Native Codex plugin + marketplace + AGENTS + Atelier wrapper workflow

---

## Quick Install

```bash
make install
```

By default this installs Codex user/global config. For a project-local install:

```bash
bash scripts/install_codex.sh --workspace /path/to/workspace
```

---

## What Gets Installed

| Artifact                 | Global install                       | `--workspace DIR` install                      |
| ------------------------ | ------------------------------------ | ---------------------------------------------- |
| Codex plugin source      | `~/.codex/plugins/atelier/`          | `<workspace>/.codex/plugins/atelier/`          |
| Marketplace file         | `~/.agents/plugins/marketplace.json` | `<workspace>/.agents/plugins/marketplace.json` |
| AGENTS instruction block | `~/.codex/AGENTS.md`                 | `<workspace>/AGENTS.md`                        |
| Wrapper script           | `~/.local/bin/atelier-codex`         | `<workspace>/bin/atelier-codex`                |
| Task templates           | not installed globally               | `<workspace>/.codex/tasks/*.md`                |

The installer also writes a generated `servers/atelier-mcp-wrapper.sh` inside
the installed plugin source and patches the plugin `.mcp.json` to use that
repo-pinned wrapper.

## Verify

```bash
make verify
```

## First Task

Start Codex in your workspace, restart once so the marketplace is reloaded, and use the bundled skill:

```text
use skill: atelier-lint
```

Or run the Atelier preflight wrapper:

```bash
./bin/atelier-codex --task "Fix live state drift" --domain state.change
```

## Expected Behavior

- Codex finds the local `atelier` marketplace and installs the plugin source on restart
- Codex loads bundled Atelier skills and connects to the generated MCP wrapper
- Wrapper preflight always runs reasoning context, plan validation, and optional rubric gate

## Troubleshooting

| Problem              | Fix                                                                                                            |
| -------------------- | -------------------------------------------------------------------------------------------------------------- |
| Plugin not visible   | Restart Codex, then check `~/.agents/plugins/marketplace.json` or workspace `.agents/plugins/marketplace.json` |
| MCP tools missing    | Verify `<plugin>/servers/atelier-mcp-wrapper.sh` exists inside the installed plugin source                     |
| Wrapper missing      | Re-run install and verify global `atelier-codex` or workspace `bin/atelier-codex` exists                       |
| Skills look outdated | Re-run `bash scripts/install_codex.sh` to refresh the copied plugin source                                     |

## V2 Tools — Memory, Context Savings, and Lesson Pipeline

The following V2 tools are available via MCP once installed. All are **Atelier augmentations** — native Codex read/search tools remain the primary interface.

| Tool                    | Boundary                                         | Description                                       |
| ----------------------- | ------------------------------------------------ | ------------------------------------------------- |
| `memory`                | Atelier augmentation                             | Store named value in agent memory                 |
| `memory`                | Atelier augmentation                             | Retrieve named memory block                       |
| `memory`                | Atelier augmentation                             | FTS + vector search over archival memory          |
| `memory`                | Atelier augmentation                             | Persist text passage to archival memory           |
| `memory`                | Atelier augmentation                             | Compact sleeptime memory (reduces context window) |
| `search`                | Atelier augmentation                             | Token-saving combined search + read               |
| `edit`                  | Atelier augmentation                             | Deterministic multi-file batch edits (optional)   |
| `atelier bench runtime` | Atelier augmentation                             | Capability efficiency metrics                     |
| `compact`               | Atelier augmentation over host-native `/compact` | Advise before compaction; provides reinject hints |
| `atelier lesson inbox`  | Atelier augmentation                             | List lesson candidates awaiting decision          |
| `atelier lesson decide` | Atelier augmentation                             | Approve or reject a lesson candidate              |

See `integrations/codex/tasks/preflight.md` for how to use `memory` and `search` in the preflight workflow.

## Uninstall

```bash
bash scripts/uninstall_codex.sh
bash scripts/uninstall_codex.sh --workspace /path/to/workspace
```
