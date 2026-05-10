# Atelier Docs

This docs index is organized around the installed product first, then integration, then contributor material.

## Start Here

If you are trying to use Atelier as a product, start with these three pages:

| Doc                                      | What it covers                                                      |
| ---------------------------------------- | ------------------------------------------------------------------- |
| [installation.md](installation.md)       | Install script, installed commands, runtime modes, storage backends |
| [quickstart.md](quickstart.md)           | 5-minute walkthrough using installed `atelier` commands             |
| [troubleshooting.md](troubleshooting.md) | Common install, runtime, stack, and servicectl issues               |

## Installed Runtime

These pages cover normal day-to-day use after installation:

| Doc                                      | What it covers                                         |
| ---------------------------------------- | ------------------------------------------------------ |
| [cli.md](cli.md)                         | Full CLI command reference                             |
| [installation.md](installation.md)       | `servicectl`, `stack`, optional HTTP service, env vars |
| [troubleshooting.md](troubleshooting.md) | PATH, Docker, MCP, servicectl, auth issues             |

## Connect an Agent Host

Use these when wiring Atelier into an editor or agent CLI:

| Doc                                                          | Host                                        |
| ------------------------------------------------------------ | ------------------------------------------- |
| [hosts/claude-code-install.md](hosts/claude-code-install.md) | Claude Code                                 |
| [hosts/copilot-install.md](hosts/copilot-install.md)         | VS Code Copilot                             |
| [hosts/codex-install.md](hosts/codex-install.md)             | Codex CLI                                   |
| [hosts/opencode-install.md](hosts/opencode-install.md)       | opencode                                    |
| [hosts/gemini-cli-install.md](hosts/gemini-cli-install.md)   | Gemini CLI                                  |
| [hosts/all-agent-clis.md](hosts/all-agent-clis.md)           | Overview of all supported host integrations |

## Optional UI and Service

The browser UI and HTTP service are optional. These docs cover them when you need them:

| Doc                                                | What it covers                                                |
| -------------------------------------------------- | ------------------------------------------------------------- |
| [installation.md](installation.md)                 | `atelier stack start`, `atelier service start`, runtime modes |
| [engineering/service.md](engineering/service.md)   | Full HTTP service reference and operations detail             |
| [production-readiness.md](production-readiness.md) | Production deployment and operations checklist                |

## Authoring and Operations

These pages are for teams extending their own ReasonBlocks, rubrics, and operational setup:

| Doc                                                                              | What it covers                 |
| -------------------------------------------------------------------------------- | ------------------------------ |
| [authoring/reasonblock-authoring.md](authoring/reasonblock-authoring.md)         | ReasonBlock format             |
| [authoring/rubric-authoring.md](authoring/rubric-authoring.md)                   | Rubric format                  |
| [authoring/failure-cluster-authoring.md](authoring/failure-cluster-authoring.md) | Failure cluster format         |
| [packs.md](packs.md)                                                             | Bundle format and CLI commands |

## Contributors and Engineering

Everything below is intentionally contributor-oriented rather than end-user oriented:

| Doc                                                        | What it covers                                |
| ---------------------------------------------------------- | --------------------------------------------- |
| [engineering/contributing.md](engineering/contributing.md) | Source checkout, test, lint, release workflow |
| [engineering/storage.md](engineering/storage.md)           | SQLite vs PostgreSQL internals                |
| [engineering/mcp.md](engineering/mcp.md)                   | MCP implementation details                    |
| [engineering/security.md](engineering/security.md)         | Threat model and controls                     |
| [engineering/evals.md](engineering/evals.md)               | Eval and benchmark internals                  |
| [architecture/runtime.md](architecture/runtime.md)         | Runtime architecture                          |
| [architecture/](architecture/)                             | Design docs and implementation plans          |

## Quick Reference

Installed product flow:

```bash
atelier --version
atelier servicectl status
atelier lint --task "..." --domain "..." --step "..."
atelier reasoning --task "..." --domain "..."
atelier stack start
```

Contributor/source flow:

```bash
cd atelier
uv sync --all-extras
uv run atelier init
make verify
```
