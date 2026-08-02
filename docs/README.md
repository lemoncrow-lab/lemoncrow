# LemonCrow Documentation

Welcome to [LemonCrow](https://github.com/lemoncrow-lab/lemoncrow) — the open-source (Apache-2.0), local-first context and execution runtime for coding agents. LemonCrow is a fully local, account-free tool in low-maintenance mode.

LemonCrow keeps existing agents sharp on real codebases with a ranked code graph, exact-range tools, bounded output, durable memory, verification, and auditable runtime controls across Claude Code, Codex, Copilot, opencode, LangChain, and MCP-compatible hosts.

## Setup & Troubleshooting

| Document                        | Description                                                                        |
| ------------------------------- | ---------------------------------------------------------------------------------- |
| [Installation](./setup/installation.md) | Install script, background services, modes, storage, and uninstall          |
| [Privacy & Network](./setup/privacy.md) | What runs locally, network behavior, and telemetry (on by default; `lc telemetry remote off`) |
| [Troubleshooting](./setup/troubleshooting.md) | Common install, runtime, and background service issues                  |

## Host Integrations

| Host                                                        | Document                                |
| ----------------------------------------------------------- | --------------------------------------- |
| [All Hosts Overview](./hosts/all-agent-clis.md)             | Overview of supported host integrations |
| [Host Capability Matrix](./hosts/host-capability-matrix.md) | Cross-host capability contract          |
| [Claude Code](./hosts/claude-code-install.md)               | Claude Code setup                       |
| [Copilot](./hosts/copilot-install.md)                       | Copilot setup                           |
| [Codex CLI](./hosts/codex-install.md)                       | Codex CLI setup                         |
| [opencode](./hosts/opencode-install.md)                     | opencode setup                          |
| [Antigravity](./hosts/antigravity-install.md)               | Antigravity setup                       |
| [Cursor](./hosts/cursor-install.md)                         | Cursor setup                            |
| [Hermes Agent](./hosts/hermes-install.md)                   | Hermes Agent setup                      |

## SDK & API

| Document                      | Description                |
| ----------------------------- | -------------------------- |
| [Python SDK](./sdk/python.md) | Embedded Python client     |
| [MCP Server](./sdk/mcp.md)    | Current MCP server surface |

## Reference

| Document                                      | Description                                              |
| --------------------------------------------- | -------------------------------------------------------- |
| [CLI Reference](./reference/cli.md)           | Current public CLI reference                             |
| [Architecture](./reference/architecture.md)   | System architecture                                     |
| [OpenAI Gateway](./reference/openai-gateway.md) | OpenAI-compatible gateway surface                     |

## Benchmarks

| Document                              | Description                                                             |
| ------------------------------------- | ----------------------------------------------------------------------- |
| [Benchmark Results](./benchmarks/results.md) | Every suite, every number, every raw-run link — and the retrieval eval vs 10 named code-search tools |

## Legal & Licensing

| Document                                            | Description                                                     |
| --------------------------------------------------- | --------------------------------------------------------------- |
| [Licensing](./legal/licensing.md)                  | Apache-2.0 in full and the optional (gates-nothing) account     |
| [Licensing report](./legal/licensing-report.md)    | Licensing position for the maintenance-mode / open-source transition |
| [Runtime dependency licenses](./legal/dependency-licenses.md) | Auto-generated SPDX dependency license list              |

## Roadmap & Savings

| Document                                                      | Description                                                     |
| ------------------------------------------------------------- | --------------------------------------------------------------- |
| [Roadmap](./planning/roadmap.md)                              | Shipped capabilities and possible technical directions          |
| [Savings optimization status](./planning/savings-optimization-roadmap.md) | Six-lever implementation audit, evidence, acceptance gates, and next work |

## Operations

| Document                                                            | Description                                              |
| ------------------------------------------------------------------- | -------------------------------------------------------- |
| [Self-hosting notes](./operations/production-readiness.md)          | Optional operational notes for running the local service  |
| [Maintenance-mode transition](./operations/maintenance-mode-transition.md) | Background on the move to a fully local runtime |

---

**Links:** [GitHub](https://github.com/lemoncrow-lab/lemoncrow) · [Privacy](./setup/privacy.md) · [License](../LICENSE)
