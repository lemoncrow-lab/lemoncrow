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
the installed plugin source, patches the plugin `.mcp.json` to use that
repo-pinned wrapper, and merges the Atelier Codex instructions into an
existing `AGENTS.md` instead of failing when one is already present.

## Verify

```bash
make verify
```

## First Task

Start Codex in your workspace, restart once so the marketplace is reloaded, and run the bundled status command:

```text
/atelier:status
```

Or run the Atelier preflight wrapper:

```bash
./bin/atelier-codex --task "Fix live state drift" --domain state.change
```

## Expected Behavior

- Codex finds the local `atelier` marketplace and installs the plugin source on restart
- Codex loads bundled Atelier skills and connects to the generated MCP wrapper
- Codex talks to the local Atelier HTTP service at `http://127.0.0.1:8787` by default
- The service owns the shared central store; Codex MCP does not directly write workspace `.atelier` state
- Wrapper preflight records task context and can run an optional rubric gate

## Troubleshooting

| Problem              | Fix                                                                                                            |
| -------------------- | -------------------------------------------------------------------------------------------------------------- |
| Plugin not visible   | Restart Codex, then check `~/.agents/plugins/marketplace.json` or workspace `.agents/plugins/marketplace.json` |
| MCP tools missing    | Verify `<plugin>/servers/atelier-mcp-wrapper.sh` exists inside the installed plugin source                     |
| Wrapper missing      | Re-run install and verify global `atelier-codex` or workspace `bin/atelier-codex` exists                       |
| Skills look outdated | Re-run `bash scripts/install_codex.sh` to refresh the copied plugin source                                     |

## V2 Tools — Memory, Context Savings, and Lesson Pipeline

With `ATELIER_DEV_MODE=1`, the active Atelier MCP surface for Codex includes
`context`, `route`, `rescue`, `trace`, `verify`, `memory`, `read`, `edit`,
`sql`, `search`, `compact`, `shell`, and the `code` helpers.

Without developer mode, `trace` remains the most reliable active surface and
some other tools may still appear as passive compatibility stubs.

See `integrations/codex/tasks/preflight.md` for how to use `memory` and `search` in the preflight workflow.

## Uninstall

```bash
bash scripts/uninstall_codex.sh
bash scripts/uninstall_codex.sh --workspace /path/to/workspace
```
