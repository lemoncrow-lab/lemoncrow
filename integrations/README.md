# Atelier Host Integrations

Canonical adapter configs and install/verify scripts for every supported agent CLI.

## Supported Hosts

| Host        | Dir         | Support level      | Script                        |
| ----------- | ----------- | ------------------ | ----------------------------- |
| Claude Code | `claude/`   | Full plugin        | `scripts/install_claude.sh`   |
| Codex       | `codex/`    | Skills + MCP       | `scripts/install_codex.sh`    |
| opencode    | `opencode/` | MCP config         | `scripts/install_opencode.sh` |
| Copilot     | `copilot/`  | MCP + instructions | `scripts/install_copilot.sh`  |
| Gemini CLI  | `gemini/`   | MCP config         | `scripts/install_gemini.sh`   |

## Quick Install

From the `atelier/` directory:

```bash
# Install deps, all available agent CLIs, status helper, and runtime store
make install

# Verify code, runtime smoke tests, and integrations
make verify
```

## Per-host advanced scripts

```bash
bash scripts/install_claude.sh --dry-run
bash scripts/install_codex.sh --print-only
bash scripts/install_opencode.sh --strict
bash scripts/install_copilot.sh --workspace /path/to/workspace
bash scripts/install_gemini.sh
```

## Installer contract

- **Idempotent** — safe to run multiple times
- **Backup first** — creates timestamped backup before overwriting any user file
- **Skip if CLI absent** — prints install hint instead of failing
- **`--dry-run`** — print what would happen, touch nothing
- **`--print-only`** — print config snippet only (for manual install)
- **No secrets** — API keys and tokens are never written by install scripts
- **Absolute paths** — configs always use absolute paths, never `~` or `$HOME` in written files

## MCP command (all hosts)

All installers write the same MCP command pointing to the stable wrapper script:

```bash
<atelier_root>/scripts/atelier_mcp_stdio.sh
```

The wrapper locates the atelier repo, sets `ATELIER_ROOT`, and runs:

```
uv run python -m atelier.gateway.adapters.mcp_server
```

Never hardcodes a path; resolves at runtime from the script's own location.
