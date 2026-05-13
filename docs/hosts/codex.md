# Codex Integration

Atelier integrates with Codex via a packaged plugin, a local marketplace entry,
an AGENTS identity block, a preflight wrapper, and reusable task templates.
Installs are global by default; pass `--workspace DIR` for project-local files.

## Setup

```bash
cd atelier
uv sync --all-extras
make install
make verify
```

## Installed Artifacts

- Global: `~/.codex/plugins/atelier/`, `~/.agents/plugins/marketplace.json`, `~/.codex/AGENTS.md`, and `~/.local/bin/atelier-codex`
- Workspace: `<workspace>/.codex/plugins/atelier/`, `<workspace>/.agents/plugins/marketplace.json`, `<workspace>/AGENTS.md`, `<workspace>/bin/atelier-codex`, and `.codex/tasks/*.md`

The plugin bundles the shared Atelier skills, including the optional `openai-docs`
skill, plus a packaged `.mcp.json` config that the installer rewrites to a
repo-pinned MCP wrapper.

## Wrapper Flow

```bash
./bin/atelier-codex --task "Fix live state drift" --domain state.change
```

The wrapper talks to the local Atelier service by default and enforces:

1. `task`
2. Optional rubric gate via `--rubric`, which maps to `verify`

## MCP Tools

Canonical MCP names:

- `task`, `route`, `rescue`, `trace`, `verify`
- `memory`, `search`, `read`, `edit`, `compact`, `atelier_repo_map`

CLI-only workflows include `atelier sql inspect`, `atelier lesson inbox`, `atelier consolidation inbox`, `atelier report`, `atelier proof show`, and `atelier route contract`.

## References

Codex task and reference templates live under `integrations/codex/`.
