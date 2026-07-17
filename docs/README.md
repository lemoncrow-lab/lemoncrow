# LemonCrow Documentation

Welcome to [LemonCrow](https://github.com/lemoncrow-lab/lemoncrow) — the open-source (Apache-2.0), local-first context and execution runtime for coding agents. LemonCrow is a fully local, account-free tool in low-maintenance mode.

LemonCrow keeps existing agents sharp on real codebases with a ranked code graph, exact-range tools, bounded output, durable memory, verification, and auditable runtime controls across Claude Code, Codex, Copilot, opencode, LangChain, and MCP-compatible hosts.

## Quick Links

| Section                                           | Description                                                                                           |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| [Installation](./installation.md)                 | Install script, background services, modes, storage, and uninstall                                    |
| [Privacy & Network](./privacy.md)                 | What runs locally, network behavior, and telemetry (off by default, opt-in)                           |
| [Licensing](./licensing.md)                       | Apache-2.0 in full and the optional (gates-nothing) account                                           |
| [CLI Reference](./cli.md)                         | Current public CLI reference                                                                          |
| [Benchmark Results](./benchmarks/results.md)      | Every suite, every number, every raw-run link -- and the retrieval eval vs 10 named code-search tools |
| [Troubleshooting](./troubleshooting.md)           | Common install, runtime, and background service issues                                                |
| [Self-hosting notes](./production-readiness.md)   | Optional operational notes for running the local service yourself                                     |
| [Maintenance-mode transition](./maintenance-mode-transition.md) | Background on the move to a fully local, account-free runtime                            |

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
| [Host Capability Matrix](./hosts/host-capability-matrix.md) | Install-path and enforcement matrix     |

## SDK & API

| Document                      | Description                |
| ----------------------------- | -------------------------- |
| [Python SDK](./sdk/python.md) | Embedded Python client     |
| [MCP Server](./sdk/mcp.md)    | Current MCP server surface |

## Roadmap

| Document                | Description                                 |
| ----------------------- | ------------------------------------------- |
| [Roadmap](./roadmap.md) | Shipped capabilities and active development |

---

**Links:** [GitHub](https://github.com/lemoncrow-lab/lemoncrow) · [Privacy](./privacy.md) · [License](../LICENSE)
