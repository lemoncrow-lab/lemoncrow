# Atelier Docs

This docs index is intentionally split into a small live surface for day-to-day use and a separate `docs-archive/` tree for historical or maintainer-only material.

## Agent OS and Engineering Map

Start here if you want the repository rules, source-of-truth map, and reusable
execution scaffolding that agents should follow.

| Doc | What it covers |
| --- | --- |
| [agent-os/README.md](agent-os/README.md) | Shared agent rules, workflow, validation, and host overrides |
| [architecture/README.md](architecture/README.md) | Live repository architecture map |
| [design/index.md](design/index.md) | Core beliefs behind the repo shape |
| [frontend/README.md](frontend/README.md) | Frontend-specific guidance and validation |
| [reliability/README.md](reliability/README.md) | Reusable validation and evidence loops |
| [security/README.md](security/README.md) | Durable security rules for agent work |
| [quality/scorecard.md](quality/scorecard.md) | Current repo quality gaps and targets |
| [plans/README.md](plans/README.md) | Durable execution plans and tech debt |
| [decisions/README.md](decisions/README.md) | Architectural decision records |
| [references/README.md](references/README.md) | High-signal source files and contracts |

## Product & Strategy

Start here if you want to understand what Atelier is for, where it's going, and how to execute on it.

| Doc                                                | What it covers                                          |
| -------------------------------------------------- | ------------------------------------------------------- |
| [product/EXECUTION-SUMMARY.md](product/EXECUTION-SUMMARY.md) | One-page summary for new team members            |
| [product/STRATEGY.md](product/STRATEGY.md)         | Positioning, market gap, defensible moat                |
| [product/ROADMAP.md](product/ROADMAP.md)           | Execution-ordered 2-week / 30-day / 90-day plan         |
| [product/PRICING.md](product/PRICING.md)           | Tiers, business model, revenue targets                  |
| [product/GTM.md](product/GTM.md)                   | Go-to-market and content strategy                       |
| [specs/README.md](specs/README.md)                 | Per-feature execution specs (for coding agents)         |

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
| [hosts/host-capability-matrix.md](hosts/host-capability-matrix.md) | Cross-host capability contract     |
| [hosts/claude-code-install.md](hosts/claude-code-install.md) | Claude Code                             |
| [hosts/copilot-install.md](hosts/copilot-install.md)         | Copilot                                 |
| [hosts/codex-install.md](hosts/codex-install.md)             | Codex CLI                               |
| [hosts/opencode-install.md](hosts/opencode-install.md)       | opencode                                |
| [hosts/antigravity-install.md](hosts/antigravity-install.md) | Antigravity                             |
| [integrations/host-matrix.md](integrations/host-matrix.md)   | Install-path and enforcement matrix     |

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
atelier tools call context --dev --args '{"task":"...","domain":"..."}' --json
atelier tools call verify --dev --args '{"rubric_id":"rubric_state_change_safety","checks":{"canonical_identifier_used":true}}' --json
```

Contributor/source flow:

```bash
cd atelier
uv sync --all-extras
atelier init
make verify
```
