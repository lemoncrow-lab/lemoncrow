# LemonCrow Documentation

LemonCrow is infrastructure around coding agents: repository intelligence and tools while the agent works, plus a Review surface for understanding what changed afterwards.

The product has three public layers:

- **Runtime** — use LemonCrow with the coding agent you already have. Claude Code, Codex, OpenCode, and Cursor are supported external integrations.
- **Review** — inspect agent-written changes, evidence, discussion, and durable human review state.
- **Enterprise** — shared infrastructure, policy, managed workspaces, organization-wide visibility, and broader agent workloads.

Runtime Lite stays local through existing-agent integrations and `lc runtime index`. `lc code` is the Hosted LemonCrow-owned coding agent. A Pro plan grants Hosted Runtime access; advanced private intelligence and owned execution are Hosted/Enterprise capabilities, not a second local engine.

## Start here

Install LemonCrow:

```bash
curl -fsSL https://lemoncrow.com/install.sh | bash
```

Use it with an existing agent:

```bash
lc with claude
lc with codex
lc with opencode
```

Those commands add LemonCrow to that launch without changing the agent's saved model, credentials, or settings. Cursor uses its installed LemonCrow integration instead of `lc with`.

To use LemonCrow's Hosted owned coding agent, the CLI entry point is:

```bash
lc code
```

For local Runtime Lite, use `lc with claude|codex|opencode|pi`, Cursor's installed integration, or a persistent `lc rt enable <host>` integration.

Open Review:

```bash
lc review --open
```

## Product docs

| Document | What it covers |
| --- | --- |
| [Installation](./setup/installation.md) | Install, first run, external-agent integrations, `lc code`, and uninstall |
| [LemonCrow Pro: Hosted Runtime](./setup/pro-hosted-runtime.md) | Pro onboarding: install, `lc rt enable --hosted`, sign-in, device approval, and checking hosted search |
| [Privacy & network](./setup/privacy.md) | What stays local, what can make network calls, and telemetry |
| [Troubleshooting](./setup/troubleshooting.md) | Common installation and runtime problems |
| [Runtime Lite architecture](./architecture/runtime-lite.md) | Local repository intelligence and the local/managed boundary |
| [OSS/private intelligence boundary](./architecture/oss-private-code-intelligence.md) | Which code-intelligence capabilities are public versus private |
| [Review authority map](./architecture/review-authority-map.md) | Which code owns each Review decision, and the rule that trust features never add a second authority |
| [`lc review`](./reference/review.md) | Review concepts and the core review loop |
| [Review walkthrough](./reference/review-walkthrough.md) | Hands-on Review tour |
| [CLI reference](./reference/cli.md) | Current public CLI |
| [Support matrix](./reference/support-matrix.md) | What is supported today, and where: capabilities, platforms, browsers, source hosts and deployment |

## Coding-agent integrations

| Integration | How LemonCrow fits |
| --- | --- |
| [Claude Code](./hosts/claude-code-install.md) | LemonCrow plugin; temporary `lc with claude` or persistent integration |
| [Codex](./hosts/codex-install.md) | LemonCrow plugin; temporary `lc with codex` or persistent integration |
| [OpenCode](./hosts/opencode-install.md) | LemonCrow plugin; temporary `lc with opencode` or persistent integration |
| [Cursor](./hosts/cursor-install.md) | Installed IDE/CLI integration with lifecycle hooks |
| [Pi](./hosts/pi-install.md) | Public managed coding-agent host; install with `lc rt host ... --engine pi`, try with `lc with pi` |
| [LemonCode](./hosts/lemoncode-install.md) | Alternative LemonCrow-managed frontend |
| [All integrations](./hosts/all-agent-clis.md) | Full integration overview |
| [Capability matrix](./hosts/host-capability-matrix.md) | Technical differences between hosts |

## Advanced and developer reference

| Document | What it covers |
| --- | --- |
| [Architecture reference](./reference/architecture.md) | Detailed internal architecture |
| [Python SDK](./sdk/python.md) | Embedded Python client |
| [Protocol / MCP reference](./sdk/mcp.md) | Low-level protocol surface for integration authors |
| [Production/self-hosting notes](./operations/production-readiness.md) | Running shared services and production deployments |
| [Benchmark results](./benchmarks/results.md) | Published evaluations and raw-run links |

## Historical documents

`docs/planning/` and `docs/reports/` are implementation history. They intentionally preserve the architecture and terminology that existed when each plan or report was written. Use the current setup, host, architecture, and reference docs above for present product behavior.

---

[GitHub](https://github.com/lemoncrow-lab/lemoncrow) · [Runtime](https://lemoncrow.com/runtime) · [Review](https://lemoncrow.com/review) · [Enterprise](https://lemoncrow.com/enterprise) · [Privacy](./setup/privacy.md)
