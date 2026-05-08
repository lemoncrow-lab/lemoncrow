# atelier-codex — Codex plugin + marketplace integration

Atelier now ships a real Codex plugin template under `integrations/codex/plugin`
and a repo marketplace at `.agents/plugins/marketplace.json`.

## Layout

```
atelier/
├── .agents/plugins/marketplace.json   # repo-scoped marketplace for Codex
├── integrations/codex/
│   ├── plugin/                        # Codex plugin template
│   │   ├── .codex-plugin/plugin.json
│   │   ├── .mcp.json
│   │   └── skills/
│   └── tasks/                         # workspace preflight task templates
└── integrations/skills/               # shared source-of-truth skills
```

## Install

1. Install the engine:
   ```bash
   cd atelier
   uv sync
   uv run atelier init
   ```
2. Install the Codex integration:
   ```bash
   bash scripts/install_codex.sh
   ```
   Or for a project-local install:
   ```bash
   bash scripts/install_codex.sh --workspace /path/to/workspace
   ```
3. The installer copies the Codex plugin template into a local plugin source,
   generates a repo-pinned MCP wrapper inside that source, and writes a
   personal or workspace marketplace entry that points at `./.codex/plugins/atelier`.

For direct repo testing without the installer, add the repo marketplace from the
Atelier root:

```bash
codex plugin marketplace add .
```

That repo marketplace points at `./integrations/codex/plugin` and is useful
when `atelier-mcp` is already available on your `PATH`.

## Usage

- `atelier-task` remains the default coding-task loop and drives the Atelier
  reasoning workflow.
- The `atelier-codex` preflight wrapper now runs `reasoning`, then `lint`, then
  optional `verify` before handing off to Codex.

## Hard rules

- Never edit before `lint` returns `ok`.
- Never retry a failing command a third time without `rescue`.
- Never declare success on high-risk domains without `verify`.
- Never record secrets or hidden chain-of-thought.
