# Copilot Integration

Atelier integrates with Copilot Chat through VS Code MCP config, instruction injection, chat mode, and task presets. Installs are global by default; pass `--workspace DIR` for project-local files.

## Setup

```bash
cd atelier
uv sync --all-extras
make install
make verify
```

## Installed Artifacts

- Global: VS Code user `mcp.json`, `~/.copilot/instructions/atelier.instructions.md`, and VS Code user `tasks.json`
- Workspace: `.vscode/mcp.json`, `.github/copilot-instructions.md`, `.github/chatmodes/atelier.chatmode.md`, and `.vscode/tasks.json` when `--workspace DIR` is used

## Usage

In Copilot Chat, ask explicitly for Atelier MCP tools when you need task context or rubric checks.

Example:

```text
Use `task` before editing files.
```

## MCP Tool Names

Canonical MCP names: `task`, `route`, `rescue`, `trace`, `verify`, `memory`, `read`, `edit`, `search`, `compact`, `atelier_repo_map`.

CLI-only workflows include `atelier lesson inbox`, `atelier consolidation inbox`, `atelier report`, and `atelier proof show`.

## Fallback

If MCP is unavailable, use copy-paste context guidance in `docs/copy-paste/copilot-instructions.md`.
