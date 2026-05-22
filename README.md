# Atelier — Open-Source Context Runtime

**Reusable engineering judgment for AI-assisted coding.**

Make your best engineers’ judgment available to junior engineers and AI agents.

Atelier helps teams ship the same context runtime everywhere: on the command line, inside agent hosts through MCP, and with a detached background agents.

The runtime separates **Passive Tracking** (enabled by default) from **Active Context** (requires Development Mode).

![Atelier Overview dashboard](docs/assets/screenshots/overview.png)

## Passive Tracking (Production Ready)

- **Sessions & Ledger** — track every agent run and execution state
- **Expense Tracking** — aggregate token usage and estimated costs across all hosts
- **Traces** — record observable execution history (files, commands, errors)
- **Tools & Agents** — central registry of available capabilities and personas

![Cost & efficiency analytics across hosts, models, and tools](docs/assets/screenshots/analytics.png)

## Active Context (Development Mode)

_Enable with `ATELIER_DEV_MODE=1`_

- **Context** — retrieve and inject known procedures (ReasonBlocks) into agent context
- **Watchdogs** (formerly Monitors) — detect execution pathologies (loops, thrashing) and suggest rescues
- **Rubric verification** — gate agent outputs against domain-specific safety checks
- **Context Optimization** — smart tool selection and token-reduction
- **Failure rescue** — surface targeted rescue procedures for recurring failures

## Install in One Command

```bash
curl -fsSL https://raw.githubusercontent.com/pankaj4u4m/atelier/main/scripts/install.sh | bash
```

The installer does four things by default:

- installs `atelier` and `atelier-mcp` as user-level console commands in `~/.local/bin`
- initializes the runtime store under `~/.atelier`
- starts the detached `servicectl` background loop
- attempts to start the optional visualization stack when npm is available and `ATELIER_NO_STACK=1` is not set
- installs supported host integrations when the host CLI is found on `PATH`

Check the installed runtime:

```bash
atelier --version
atelier-mcp --version
atelier background status
```

→ User install guide: [docs/installation.md](docs/installation.md)

## What Runs After Install

The installed product gives you **CLI + MCP + Background Services**.

Atelier automatically registers itself with your OS background manager (**systemd** on Linux, **launchd** on macOS). This ensures it starts on boot and restarts automatically if it crashes.

- `atelier ...` works with no HTTP server.
- `atelier-mcp` works with no HTTP server.
- `atelier background ...` manages the OS-level services (controller + stack).
- `atelier stack ...` manages the optional API + frontend processes manually.

Pure CLI mode still emits Atelier telemetry events unless you disable it with `atelier telemetry off` or `ATELIER_TELEMETRY=0`.

## Daily Use

Atelier follows a simple **3-step process** for reliable coding:

1. **Context**: Retrieve procedures and facts before starting.
2. **Implement**: Execute the task (with optional rescue/route).
3. **Record**: Record the outcome once done.

Passive tracking works automatically. Active context features require `ATELIER_DEV_MODE=1`.

```bash
# Fetch context for an agent task (Requires Dev Mode)
atelier tools call context --dev --args '{
  "task": "Fix generated output that drifts back after refresh",
  "domain": "source.truth",
  "files": ["src/content/generate.py"]
}' --json

# Verify required checks after a task completes (Requires Dev Mode)
atelier tools call verify --dev --args '{
  "rubric_id": "rubric_state_change_safety",
  "checks": {
    "canonical_identifier_used": true,
    "pre_change_state_captured": true,
    "read_after_write_completed": true
  }
}' --json
```

Common runtime commands:

```bash
atelier background status
atelier worker list
atelier runs list
atelier tools call search --dev --args '{"query":"read after write verification"}' --json
```

→ Installed quickstart: [docs/quickstart.md](docs/quickstart.md)

## Optional UI Stack

The UI is optional. The installer registers it as a background service by default when npm is available. You can manage it alongside the controller:

```bash
# View logs for the visualization stack
atelier background logs stack

# Restart the entire environment
atelier background restart
```

Then open:

- frontend: [http://localhost:3125](http://localhost:3125)
- service API: [http://localhost:8787](http://localhost:8787)

## Background Processing & Auto-Update

Atelier includes an offline processing controller that runs as a background service. It handles session imports, maintenance jobs, and **automatic code updates**.

The controller periodically checks your git repository for updates. When found, it automatically pulls the latest code, syncs dependencies, and restarts the services.

```bash
# Check service health and auto-update status
atelier background status

# View background logs (from journald/launchd)
atelier background logs controller

# Manually trigger a restart
atelier background restart
```

You can also queue and process work manually:

```bash
atelier worker enqueue consolidate_reasonblocks
atelier worker run-once
atelier worker list
```

## Connect an Agent Host

The installer already attempts host integration when the relevant CLI is present. If you want to review or customize host setup, use the per-host docs:

- Claude Code: MCP + skills + agents — [docs/hosts/claude-code-install.md](docs/hosts/claude-code-install.md)
- Codex CLI: MCP + AGENTS.md — [docs/hosts/codex-install.md](docs/hosts/codex-install.md)
- Copilot: MCP + instructions — [docs/hosts/copilot-install.md](docs/hosts/copilot-install.md)
- opencode: MCP — [docs/hosts/opencode-install.md](docs/hosts/opencode-install.md)
- Antigravity: MCP — [docs/hosts/antigravity-install.md](docs/hosts/antigravity-install.md)

→ Full host overview: [docs/hosts/all-agent-clis.md](docs/hosts/all-agent-clis.md)

## What Atelier Provides

- **Context reuse**: retrieve and inject known procedures before or during complex tasks
- **Semantic memory**: FTS + optional vector search over procedures and traces
- **Loop detection**: detect thrashing, second-guessing, and repeated failures
- **Tool supervision**: cached reads, memoized searches, injection-guarded grep
- **Context compression**: summarise long-running ledgers into reusable state
- **Rubric verification**: enforce required checks before and after risky work
- **Failure rescue**: surface targeted recovery procedures from recurring failures

## Docs by Audience

- End users: [docs/installation.md](docs/installation.md), [docs/quickstart.md](docs/quickstart.md), [docs/troubleshooting.md](docs/troubleshooting.md)
- Integrators: [docs/hosts/](docs/hosts/), [docs/sdk/mcp.md](docs/sdk/mcp.md), [docs/sdk/python.md](docs/sdk/python.md)
- Contributors: [docs/engineering/contributing.md](docs/engineering/contributing.md)

Deeper maintainer history, internal design notes, benchmark history, and legacy integration material now live under `docs-archive/` rather than the primary docs tree.

→ Full documentation index: [docs/README.md](docs/README.md)

## For Developers and Contributors

If you are working from a source checkout instead of the installed product:

```bash
cd atelier
uv sync --all-extras
atelier init
make verify
```

Developer-focused references:

- CLI reference: [docs/cli.md](docs/cli.md)
- MCP reference: [docs/sdk/mcp.md](docs/sdk/mcp.md)
- Contributing guide: [docs/engineering/contributing.md](docs/engineering/contributing.md)

Archived maintainer references are preserved under `docs-archive/engineering/` and `docs-archive/architecture/`.

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

→ SDK reference: [docs/sdk/python.md](docs/sdk/python.md)

## Architecture and Storage

Atelier is a context runtime, not an agent framework and not a general-purpose vector database.

```text
Agent Host (Claude Code / Codex / Copilot / opencode / Antigravity)
        |
        |  MCP stdio  (or CLI / Python SDK)
        v
Atelier Runtime
|- ReasonBlock store   (SQLite + FTS5, optional pgvector)
|- Rubric gates        (domain-specific verification rules)
|- Run ledger          (per-session execution state)
|- Failure clusters    (recurring error signatures -> rescue procedures)
|- Context compressor  (ledger summarisation)
`- Tool cache          (read / search / edit)
        |
        |- Local SQLite (default)
        `- PostgreSQL   (optional, ATELIER_DATABASE_URL)
```

Default storage layout:

| Path                      | Contents                                           |
| ------------------------- | -------------------------------------------------- |
| `.atelier/atelier.db`     | SQLite store for blocks, traces, rubrics, and jobs |
| `.atelier/blocks/*.md`    | Markdown mirror of ReasonBlocks                    |
| `.atelier/traces/*.json`  | JSON mirror of recorded traces                     |
| `.atelier/rubrics/*.yaml` | YAML mirror of rubrics                             |

→ Full storage and environment reference: [docs/installation.md](docs/installation.md)

## Safety

- No chain-of-thought storage — only observable fields like commands, errors, and summaries
- Redaction is applied before trace persistence
- API keys and host tokens are not written to the store
- Hooks remain opt-in for host integrations

→ Archived security details: `docs-archive/engineering/security.md`

## Benchmarks and Repository Layout

Benchmarks, engineering internals, and repo structure live below the user journey on purpose.

- Benchmarks archive: `docs-archive/benchmarks/`
- Engineering internals archive: `docs-archive/engineering/`
- Contributor quick reference: [QUICK_REFERENCE.md](QUICK_REFERENCE.md)

| Path            | Purpose                                                    |
| --------------- | ---------------------------------------------------------- |
| `src/atelier/`  | Runtime, CLI, MCP server, service, store, and capabilities |
| `tests/`        | pytest suite                                               |
| `docs/`         | user, integration, and engineering documentation           |
| `integrations/` | host adapter configs and install/verify scripts            |
| `frontend/`     | optional React + Vite visualization stack                  |
