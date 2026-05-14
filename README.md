# Atelier - Open-Source Context Runtime

Reusable engineering judgment for AI-assisted coding.

Atelier gives coding agents and human teams a shared local runtime for context,
trace history, failure rescue, rubric checks, and host integrations. The default
install is local-first: CLI commands, an MCP server for agent hosts, a SQLite
runtime store, and a detached background worker. The browser UI and HTTP service
are optional.

## Quick Start

Use this path if you just landed in the repo and want Atelier running.

### 1. Prerequisites

- Linux or macOS shell
- `curl`
- `git`
- Python 3.11+ for source checkout work
- Docker, optional, only for the visualization stack
- `uv`, optional for installed-product users and recommended for source checkout
  development; the installer can install it when needed

### 2. Install

```bash
curl -fsSL https://raw.githubusercontent.com/pankaj4u4m/atelier/main/scripts/install.sh | bash
```

The installer:

- installs `atelier` and `atelier-mcp` as user-level commands in `~/.local/bin`
- clones or updates Atelier under `~/.local/share/atelier`
- initializes the runtime store under `~/.atelier`
- starts the detached `servicectl` background loop
- tries to start the optional UI/API stack if Docker is available
- installs supported agent-host integrations when the host CLI is on `PATH`

Installer details and variants: [docs/installation.md](docs/installation.md)

### 3. Verify

```bash
atelier --version
atelier-mcp --version
atelier servicectl status
atelier stack status
```

Expected result:

- `atelier` and `atelier-mcp` resolve on `PATH`
- `servicectl` reports the background controller status
- `atelier stack status` reports whether the optional Docker UI/API stack is
  running

### 4. Try the First Useful Commands

```bash
atelier -h
atelier help servicectl
atelier trace list --limit 5
atelier worker list
```

Active context commands are gated behind Development Mode:

```bash
ATELIER_DEV_MODE=1 atelier context \
  --task "Fix generated output that drifts back after refresh" \
  --domain source.truth \
  --file src/content/generate.py
```

Record an observable result:

```bash
echo '{
  "agent": "quickstart",
  "domain": "state.change",
  "task": "Apply a live config update",
  "status": "partial",
  "output_summary": "Rescue requested before retrying"
}' | atelier trace record
```

Five-minute walkthrough: [docs/quickstart.md](docs/quickstart.md)

## What Atelier Is

Atelier is a context runtime for AI-assisted engineering. It is not an agent
framework and it is not a general-purpose vector database.

It provides:

- a CLI for local runtime operations
- an MCP server for Claude Code, Codex CLI, Copilot, opencode, and Gemini CLI
- a local SQLite store by default, with optional PostgreSQL and pgvector
- trace and ledger storage for observable execution history
- reusable procedures, called ReasonBlocks
- rubric checks for risky work
- rescue suggestions after repeated failures
- optional background processing and optional visualization UI

## What You Need To Know

Atelier has two operating surfaces:

| Surface | Default behavior | What it is for |
| --- | --- | --- |
| Passive Tracking | On by default | Sessions, traces, expense estimates, tool and agent registry |
| Active Context | Requires `ATELIER_DEV_MODE=1` | Context retrieval, rescue, routing, rubric verification, context optimization |

No HTTP server is required for normal CLI or MCP usage. The optional UI/API
stack is useful when you want visualization or a browser view of the runtime.

Telemetry events are emitted by default in local CLI mode. Disable them with:

```bash
atelier telemetry off
# or
ATELIER_TELEMETRY=0 atelier ...
```

## Daily Workflow

Atelier is meant to fit into a simple coding loop:

1. Get context before starting when the task is risky or unfamiliar.
2. Implement with your normal tools.
3. Record the observable result so future agents can reuse the lesson.

```bash
# Requires ATELIER_DEV_MODE=1
ATELIER_DEV_MODE=1 atelier context \
  --task "Change the generated catalog sync output" \
  --domain source.truth \
  --file src/catalog/sync.py

# Normal passive tracking command
atelier trace list --limit 10

# Requires ATELIER_DEV_MODE=1
echo '{"canonical_identifier_used": true, "read_after_write_completed": true}' \
  | ATELIER_DEV_MODE=1 atelier verify rubric_state_change_safety
```

CLI reference: [docs/cli.md](docs/cli.md)

## Runtime Commands

Background controller:

```bash
atelier servicectl status
atelier servicectl logs
atelier servicectl stop
atelier servicectl start
```

Manual worker jobs:

```bash
atelier worker list
atelier worker enqueue consolidate_reasonblocks
atelier worker run-once
```

Optional UI/API stack:

```bash
atelier stack start
atelier stack status
atelier stack logs
atelier stack stop
```

When the stack is running:

- frontend: [http://localhost:3125](http://localhost:3125)
- service API: [http://localhost:8787](http://localhost:8787)

Troubleshooting: [docs/troubleshooting.md](docs/troubleshooting.md)

## Connect An Agent Host

The installer attempts supported host integrations automatically when the host
CLI is installed. Use these docs when you want to inspect, customize, or repair a
host setup:

| Host | Setup doc |
| --- | --- |
| Claude Code | [docs/hosts/claude-code-install.md](docs/hosts/claude-code-install.md) |
| Codex CLI | [docs/hosts/codex-install.md](docs/hosts/codex-install.md) |
| Copilot | [docs/hosts/copilot-install.md](docs/hosts/copilot-install.md) |
| opencode | [docs/hosts/opencode-install.md](docs/hosts/opencode-install.md) |
| Gemini CLI | [docs/hosts/gemini-cli-install.md](docs/hosts/gemini-cli-install.md) |

Host overview: [docs/hosts/all-agent-clis.md](docs/hosts/all-agent-clis.md)

## Source Checkout Development

Use this path only if you are changing Atelier itself.

```bash
cd atelier
uv sync --all-extras
atelier init
make verify
```

Useful contributor commands:

```bash
make test-fast
make lint
make typecheck
make format
```

Contributor guide: [docs/engineering/contributing.md](docs/engineering/contributing.md)

## Python SDK

```python
from atelier.sdk import AtelierClient

client = AtelierClient.local()

context = client.get_context(
    task="Apply a live config update",
    domain="state.change",
)

rescue = client.rescue_failure(
    task="Apply a live config update",
    error="Known dead end triggered",
)
```

- SDK reference: [docs/sdk/python.md](docs/sdk/python.md)
- MCP reference: [docs/sdk/mcp.md](docs/sdk/mcp.md)

## Storage

Default local storage:

| Path | Contents |
| --- | --- |
| `~/.atelier/atelier.db` | SQLite store for blocks, traces, rubrics, jobs, and ledgers |
| `~/.atelier/blocks/` | Markdown mirrors of ReasonBlocks |
| `~/.atelier/traces/` | JSON mirrors of recorded traces |
| `~/.atelier/rubrics/` | YAML mirrors of rubrics |

PostgreSQL and pgvector are optional for shared or vector-backed deployments.
Storage and environment reference: [docs/installation.md](docs/installation.md)

## Safety Defaults

- No chain-of-thought storage; traces store observable fields such as commands,
  errors, summaries, and file references.
- Redaction runs before trace persistence.
- API keys and host tokens are not written to the runtime store.
- Host hooks remain opt-in.

Security notes: [docs-archive/engineering/security.md](docs-archive/engineering/security.md)

## Repository Map

| Path | Purpose |
| --- | --- |
| `src/atelier/` | Runtime, CLI, MCP server, service, store, and capabilities |
| `tests/` | pytest suite |
| `docs/` | live user, integration, SDK, and contributor docs |
| `docs-archive/` | historical design, benchmark, maintainer, and migration material |
| `integrations/` | host adapter configs and install/verify scripts |
| `frontend/` | optional React + Vite visualization stack |

- Full docs index: [docs/README.md](docs/README.md)
- Contributor quick reference: [QUICK_REFERENCE.md](QUICK_REFERENCE.md)
