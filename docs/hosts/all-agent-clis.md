# Atelier — All Agent CLI Integrations

Install Atelier into every supported coding agent host in one command:

```bash
make install    # install into all CLIs found on PATH
make verify     # verify code, runtime, and installed hosts
```

Installers write user/global host config by default. To install project-local
artifacts for a specific workspace, pass `--workspace DIR` to the script:

```bash
bash scripts/install_agent_clis.sh --workspace /path/to/workspace
```

---

## Supported Hosts

| Host        | Support Level                              | Advanced installer            |
| ----------- | ------------------------------------------ | ----------------------------- |
| Claude Code | Full plugin (skills, commands, hooks, MCP) | `scripts/install_claude.sh`   |
| Codex CLI   | Native MCP registration + AGENTS + plugin bundle | `scripts/install_codex.sh`    |
| opencode    | MCP + workspace agent profile              | `scripts/install_opencode.sh` |
| Copilot     | MCP + instructions + chat mode + tasks     | `scripts/install_copilot.sh`  |
| Antigravity | MCP + generated AGENTS surface             | `scripts/install_antigravity.sh` |

---

## Quickstart (install everything at once)

```bash
# 1. Make sure you're in the atelier/ directory
cd atelier

# 2. Install dependencies
uv sync --all-extras

# 3. Install into all available agent CLIs
make install

# 4. Verify
make verify
```

Hosts whose CLIs are not on PATH are skipped gracefully — this is expected in CI.

---

## Behavior Contract

All install scripts:

- Are **idempotent** — safe to run multiple times
- **Back up existing files** before any write (`.atelier-backup.TIMESTAMP`)
- **Skip gracefully** if the host CLI is not on PATH (exit 0)
- Support `--dry-run` (print actions, write nothing)
- Support `--print-only` (print manual steps for offline/audited environments)
- Support `--strict` (exit nonzero if CLI absent — useful for CI gates)
- Support `--workspace PATH` to write project-local artifacts instead of user/global config

---

## Host-Specific Install Docs

- [claude-code-install.md](claude-code-install.md)
- [codex-install.md](codex-install.md)
- [opencode-install.md](opencode-install.md)
- [copilot-install.md](copilot-install.md)
- [antigravity-install.md](antigravity-install.md)

Archived capability-contract detail now lives under `docs-archive/hosts/host-capability-matrix.md`.

---

## Integrations Layout

Detailed documentation and example configs for each host live in:

```text
atelier/integrations/
├── claude/          # Full plugin config
├── codex/           # Codex plugin template + marketplace docs
├── opencode/        # opencode.json example
├── copilot/         # .vscode/mcp.json + copilot-instructions
└── antigravity/     # Antigravity MCP template + host guidance
```

Host install entrypoints are under `scripts/install_<host>.sh`.

---

## MCP Transport

All hosts ultimately invoke the same Atelier MCP server, but packaged hosts now carry their own host-specific wrapper surfaces:

```text
atelier-mcp
```

That repo wrapper remains the canonical MCP entrypoint for direct MCP-only installs. Codex packages that same runtime behavior behind a plugin-local wrapper, while Antigravity uses workspace or user MCP configuration plus a generated host surface.

## Common MCP Surfaces

All hosts ultimately use the same Atelier stdio MCP server.

- `trace` is the consistently active observable recording surface.
- With `ATELIER_DEV_MODE=1`, hosts can actively use `context`, `route`, `rescue`,
  `verify`, `memory`, `read`, `edit`, `sql`, `search`, `compact`, `shell`, and
  the `atelier_code_*` helpers.
- Packaged hosts may add wrapper tasks, skills, or commands on top of that shared
  MCP surface.

---

## Uninstalling

Use `atelier uninstall` to remove the main Atelier install and managed host
integrations. Host-specific uninstall scripts remain available when you only want
to remove one host surface.
