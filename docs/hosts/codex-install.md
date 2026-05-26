# Installing Atelier into Codex CLI

**Support level**: Native Codex plugin + marketplace + AGENTS + directly registered Atelier MCP workflow

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
| Marketplace file         | `~/.agents/plugins/marketplace.json` | `~/.agents/plugins/marketplace.json`            |
| AGENTS instruction block | `~/.codex/AGENTS.md`                 | `<workspace>/AGENTS.md`                        |
| Codex MCP config         | `~/.codex/config.toml`               | `<workspace>/.codex/config.toml`               |
| Wrapper script           | `~/.local/bin/atelier-codex`         | `<workspace>/bin/atelier-codex`                |
| Task templates           | not installed globally               | `<workspace>/.codex/tasks/*.md`                |

The installer copies the plugin source, patches the plugin `.mcp.json` to use
`atelier-mcp --host codex` with `alwaysLoad: true`, registers Atelier in
Codex's real MCP registry via `codex mcp add`, attempts to install the Atelier
plugin via `codex plugin add` when Codex exposes the marketplace, and merges
the Atelier Codex instructions into an existing `AGENTS.md` instead of failing
when one is already present.
## Verify

```bash
make verify
```

## First Task

Start a new Codex session in your workspace, open the plugin browser with
`/plugins` if you want to confirm Atelier is installed, and switch into the
bundled explore mode:

```text
/atelier:explore
```

Or run the Atelier preflight wrapper:

```bash
./bin/atelier-codex --task "Fix live state drift" --domain state.change
```

## Expected Behavior

- Codex has a real MCP server entry for `atelier` in `config.toml`
- The installed Atelier plugin MCP config sets `alwaysLoad: true` so Codex eagerly loads the Atelier MCP server
- Codex loads the installed Atelier plugin and its bundled mode skills when the marketplace is visible to Codex
- The Codex MCP entry runs `atelier-mcp --host codex` and defaults to `ATELIER_DEV_MODE=0` (stable surface)
- Atelier persists Codex session imports and savings data under `~/.atelier/`
- The optional `atelier-codex` preflight wrapper records task context before handing off to Codex

## Troubleshooting
| Problem              | Fix                                                                                                            |
| -------------------- | -------------------------------------------------------------------------------------------------------------- |
| Plugin not visible   | Check `codex plugin list`, then verify `~/.agents/plugins/marketplace.json` points at the Atelier plugin source path; MCP registration still provides the core Atelier tool surface |
| MCP tools missing    | Verify `codex mcp list` shows `atelier`, then inspect `~/.codex/config.toml` or `<workspace>/.codex/config.toml` for `[mcp_servers.atelier]` and the installed plugin `.mcp.json` for `alwaysLoad: true` |
| Wrapper missing      | Re-run install and verify global `atelier-codex` or workspace `bin/atelier-codex` exists                       |
| Skills look outdated | Re-run `bash scripts/install_codex.sh` to refresh the copied plugin source and reinstall `atelier@atelier`     |

## V2 Tools — Memory, Context Savings, and Lesson Pipeline

With `ATELIER_DEV_MODE=1`, the active Atelier MCP surface for Codex adds
dev-only tools (`rescue`, `verify`) on top of the stable surface.

The standard Atelier install path now defaults to stable mode
(`ATELIER_DEV_MODE=0`) for the Codex MCP server entry registered in
`config.toml`.

See `integrations/codex/tasks/preflight.md` for how to use `memory` and `search` in the preflight workflow.

## Uninstall

```bash
bash scripts/uninstall_codex.sh
bash scripts/uninstall_codex.sh --workspace /path/to/workspace
```
