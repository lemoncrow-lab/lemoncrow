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
| Codex CLI   | Plugin + marketplace + AGENTS + wrapper    | `scripts/install_codex.sh`    |
| opencode    | MCP + workspace agent profile              | `scripts/install_opencode.sh` |
| Copilot     | MCP + instructions + chat mode + tasks     | `scripts/install_copilot.sh`  |
| Gemini CLI  | Extension + commands + skills + MCP        | `scripts/install_gemini.sh`   |

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
- [gemini-cli-install.md](gemini-cli-install.md)
- [host-capability-matrix.md](host-capability-matrix.md)

---

## Integrations Layout

Detailed documentation and example configs for each host live in:

```
atelier/integrations/
├── claude/          # Full plugin config
├── codex/           # Codex plugin template + marketplace docs
├── opencode/        # opencode.json example
├── copilot/         # .vscode/mcp.json + copilot-instructions
└── gemini/          # Gemini extension bundle + settings example
```

Host install entrypoints are under `scripts/install_<host>.sh`.

---

## MCP Transport

All hosts ultimately invoke the same Atelier MCP server, but packaged hosts now carry their own host-specific wrapper surfaces:

```
atelier/scripts/atelier_mcp_stdio.sh
```

That repo wrapper remains the canonical MCP entrypoint for direct MCP-only installs. Codex and Gemini package that same runtime behavior behind a plugin-local or extension-local wrapper so the host can load a packaged surface instead of a raw settings merge.

## Core Capability Tools

All hosts receive the same capability tools from the MCP server:

- `task`
- `route`
- `rescue`
- `trace`
- `verify`
- `memory`
- `read`
- `edit`
- `search`
- `compact`
- `atelier_repo_map`

---

## Uninstalling

Each host section above (see host-specific docs) has an Uninstall section. No global uninstall command — each host manages its own config location.
