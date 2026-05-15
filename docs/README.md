# Atelier Docs

This docs index is intentionally split into a small live surface for day-to-day use and a separate `docs-archive/` tree for historical or maintainer-only material.

## Primary Docs

If you are using Atelier as a product, start here and stay in this set unless you are actively contributing to the runtime itself.

| Doc                                                | What it covers                                          |
| -------------------------------------------------- | ------------------------------------------------------- |
| [installation.md](installation.md)                 | Install script, background services, modes, and storage |
| [quickstart.md](quickstart.md)                     | 5-minute walkthrough using installed `atelier` commands |
| [cli.md](cli.md)                                   | Current public CLI reference                            |
| [troubleshooting.md](troubleshooting.md)           | Common install, runtime, and background service issues  |
| [production-readiness.md](production-readiness.md) | Production and self-hosted deployment checklist         |

## Host Installs

Use these when wiring Atelier into an editor or agent CLI:

| Doc                                                          | Host                                    |
| ------------------------------------------------------------ | --------------------------------------- |
| [hosts/all-agent-clis.md](hosts/all-agent-clis.md)           | Overview of supported host integrations |
| [hosts/claude-code-install.md](hosts/claude-code-install.md) | Claude Code                             |
| [hosts/copilot-install.md](hosts/copilot-install.md)         | Copilot                                 |
| [hosts/codex-install.md](hosts/codex-install.md)             | Codex CLI                               |
| [hosts/opencode-install.md](hosts/opencode-install.md)       | opencode                                |
| [hosts/gemini-cli-install.md](hosts/gemini-cli-install.md)   | Gemini CLI                              |

## SDK And API

Keep these public because they describe supported integration surfaces:

| Doc                                                        | What it covers                       |
| ---------------------------------------------------------- | ------------------------------------ |
| [sdk/python.md](sdk/python.md)                             | Embedded Python client               |
| [sdk/mcp.md](sdk/mcp.md)                                   | Current MCP server surface           |
| [engineering/contributing.md](engineering/contributing.md) | Source-checkout contributor workflow |

## Archive And Internal

The repo still contains deeper material for maintainers, migration work, historical implementation plans, benchmark history, and internal notes. That material has been moved out of `docs/` into `docs-archive/` so the live docs tree stays small.

- `docs-archive/authoring/` and `docs-archive/packs.md` for authoring and bundle internals
- `docs-archive/engineering/` for engineering internals and service implementation detail
- `docs-archive/architecture/` for runtime plans, work packets, and historical design notes
- `docs-archive/core/` for capability and runtime internals
- `docs-archive/benchmarks/` and `docs-archive/migrations/` for benchmark and migration history
- `docs-archive/internal/` for internal notes and runbooks
- `docs-archive/integrations/`, `docs-archive/integration-*.md`, and `docs-archive/copy-paste/` for legacy integration material

## Quick Reference

Installed product flow:

```bash
atelier --version
atelier background status
ATELIER_DEV_MODE=1 atelier context --task "..." --domain "..."
echo '{"canonical_identifier_used": true}' | ATELIER_DEV_MODE=1 atelier verify rubric_state_change_safety
```

Contributor/source flow:

```bash
cd atelier
uv sync --all-extras
atelier init
make verify
```
