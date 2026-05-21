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
│   │   └── skills/                    # generated from integrations/skills
│   └── tasks/                         # workspace preflight task templates
└── integrations/skills/               # shared source-of-truth skills
```

## Install

1. Install the engine:
   ```bash
   cd atelier
   uv sync
   atelier init
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
   generates the shared skill bundle inside that source, patches the bundled
   `.mcp.json` entry to run `atelier-mcp --host codex` with
   `ATELIER_DEV_MODE=1`, registers Atelier in Codex's native MCP registry via
   `codex mcp add`, and updates the user marketplace entry that points at the
   installed plugin source.

For direct repo testing without the installer, build the generated skill bundle
first, then add the repo marketplace from the Atelier root:

```bash
make build-host-skills
codex plugin marketplace add .
```

That repo marketplace points at `./integrations/codex/plugin` and is useful
when `atelier-mcp` is already available on your `PATH`.

## Usage

- `context` is the default coding-task loop and records task context through the
  Codex-registered Atelier MCP server.
- The `atelier-codex` preflight wrapper now runs `context`, then optional `verify`
  before handing off to Codex.

## Hard rules

- Never retry a failing command a third time without `rescue`.
- Never declare success on high-risk domains without `verify`.
- Never record secrets or hidden chain-of-thought.
