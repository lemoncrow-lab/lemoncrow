# Atelier — Open-Source Reasoning Runtime

**Reusable engineering judgment for AI-assisted coding.**

Make your best engineers’ judgment available to junior engineers and AI agents.

Atelier helps teams ship the same reasoning runtime everywhere: on the command line, inside agent hosts through MCP, and with a detached background agents.

The runtime separates **Passive Tracking** (enabled by default) from **Active Reasoning** (requires Development Mode).

### Passive Tracking (Production Ready)

- **Sessions & Ledger** — track every agent run and execution state
- **Expense Tracking** — aggregate token usage and estimated costs across all hosts
- **Traces** — record observable execution history (files, commands, errors)
- **Tools & Agents** — central registry of available capabilities and personas

### Active Reasoning (Development Mode)

_Enable with `ATELIER_DEV_MODE=1`_

- **Knowledge** — retrieve and inject known procedures (ReasonBlocks) into agent context
- **Watchdogs** (formerly Monitors) — detect execution pathologies (loops, thrashing) and suggest rescues
- **Plan Lint** — validate agent plans against known dead-ends before execution
- **Rubric verification** — gate agent outputs against domain-specific safety checks
- **Context Optimization** — smart tool selection and token-reduction
- **Failure rescue** — surface targeted rescue procedures for recurring failures

## Install in One Command

```bash
curl -fsSL https://raw.githubusercontent.com/pankaj4u4m/atelier/main/scripts/install.sh | bash
```

The installer does four things by default:

- installs `atelier` and `atelier-mcp` into `~/.local/bin`
- initializes the runtime store under `~/.atelier`
- starts the detached `servicectl` background loop
- installs supported host integrations when the host CLI is found on `PATH`

Check the installed runtime:

```bash
atelier --version
atelier-mcp --version
atelier servicectl status
```

→ User install guide: [docs/installation.md](docs/installation.md)

## What Runs by Default

The installed product defaults to **CLI + MCP + background processing**.

- `atelier ...` works with no HTTP server.
- `atelier-mcp` works with no HTTP server.
- `atelier servicectl ...` manages offline/background work.
- `atelier stack ...` is optional and only needed when you want the visualization UI.

Pure CLI mode still emits Atelier telemetry events unless you disable it with `atelier telemetry off` or `ATELIER_TELEMETRY=0`.

## Daily Use

Passive tracking works automatically. Active reasoning features require `ATELIER_DEV_MODE=1`.

```bash
# Check a plan before executing it (Requires Dev Mode)
atelier lint \
  --task "Apply a live config update" \
  --domain state.change \
  --step "Resolve target from URL slug alone" \
  --step "Apply the change"

# Fetch reasoning context for an agent task (Requires Dev Mode)
atelier reasoning \
  --task "Fix generated output that drifts back after refresh" \
  --domain source.truth \
  --file src/content/generate.py

# Verify required checks after a task completes (Requires Dev Mode)
echo '{"canonical_identifier_used": true, "pre_change_state_captured": true, "read_after_write_completed": true}' \
  | atelier verify rubric_state_change_safety
```

Common runtime commands:

```bash
atelier servicectl status
atelier worker list
atelier trace list
atelier search "read after write verification"
```

→ Installed quickstart: [docs/quickstart.md](docs/quickstart.md)

## Optional UI Stack

The UI is optional. Start it only when you want visualization or a browser-based view of the runtime.

```bash
atelier stack start
```

Then open:

- frontend: [http://localhost:3125](http://localhost:3125)
- service API: [http://localhost:8787](http://localhost:8787)

Useful stack commands:

```bash
atelier stack status
atelier stack logs
atelier stack stop
```

## Background Processing

`servicectl` is the installed offline processing controller. It runs detached and periodically enqueues and processes maintenance work. It works on the default SQLite install and on Postgres-backed deployments.

```bash
atelier servicectl status
atelier servicectl logs
atelier servicectl stop
atelier servicectl start
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
- Gemini CLI: MCP — [docs/hosts/gemini-cli-install.md](docs/hosts/gemini-cli-install.md)

→ Full host overview: [docs/hosts/all-agent-clis.md](docs/hosts/all-agent-clis.md)

## What Atelier Provides

- **Reasoning reuse**: retrieve and inject known procedures before or during complex tasks
- **Semantic memory**: FTS + optional vector search over procedures and traces
- **Loop detection**: detect thrashing, second-guessing, and repeated failures
- **Tool supervision**: cached reads, memoized searches, injection-guarded grep
- **Context compression**: summarise long-running ledgers into reusable state
- **Rubric verification**: enforce required checks before and after risky work
- **Failure rescue**: surface targeted recovery procedures from recurring failures

Example:

> Agent plan: "Parse Shopify product handle from URL."
> Atelier: `status: blocked` — "Known dead end. Use Product GID. Required: re-fetch by GID + post-publish audit."

## Docs by Audience

- End users: [docs/installation.md](docs/installation.md), [docs/quickstart.md](docs/quickstart.md), [docs/troubleshooting.md](docs/troubleshooting.md)
- Integrators: [docs/hosts/](docs/hosts/), [docs/engineering/mcp.md](docs/engineering/mcp.md), [docs/engineering/service.md](docs/engineering/service.md)
- Contributors: [docs/engineering/contributing.md](docs/engineering/contributing.md), [docs/engineering/storage.md](docs/engineering/storage.md), [docs/engineering/security.md](docs/engineering/security.md)

→ Full documentation index: [docs/README.md](docs/README.md)

## For Developers and Contributors

If you are working from a source checkout instead of the installed product:

```bash
cd atelier
uv sync --all-extras
uv run atelier init
make verify
```

Developer-focused references:

- CLI reference: [docs/cli.md](docs/cli.md)
- MCP reference: [docs/engineering/mcp.md](docs/engineering/mcp.md)
- HTTP service reference: [docs/engineering/service.md](docs/engineering/service.md)
- Storage and backends: [docs/engineering/storage.md](docs/engineering/storage.md)
- Contributing guide: [docs/engineering/contributing.md](docs/engineering/contributing.md)

## Python SDK

```python
from atelier.sdk import AtelierClient

client = AtelierClient.local()

context = client.get_reasoning_context(
    task="Apply a live config update",
    domain="state.change",
)

check = client.check_plan(
    task="Apply a live config update",
    domain="state.change",
    plan=["Resolve target from URL slug alone"],
)

if check.status == "blocked":
    rescue = client.rescue_failure(
        task="Apply a live config update",
        error="Known dead end triggered",
    )
```

→ SDK reference: [docs/sdk/python.md](docs/sdk/python.md)

## Architecture and Storage

Atelier is a reasoning runtime, not an agent framework and not a general-purpose vector database.

```text
Agent Host (Claude Code / Codex / Copilot / opencode / Gemini CLI)
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

→ Security details: [docs/engineering/security.md](docs/engineering/security.md)

## Benchmarks and Repository Layout

Benchmarks, engineering internals, and repo structure live below the user journey on purpose.

- Benchmarks: [docs/benchmarks/](docs/benchmarks/)
- Engineering docs: [docs/engineering/](docs/engineering/)
- Contributor quick reference: [QUICK_REFERENCE.md](QUICK_REFERENCE.md)

| Path            | Purpose                                                    |
| --------------- | ---------------------------------------------------------- |
| `src/atelier/`  | Runtime, CLI, MCP server, service, store, and capabilities |
| `tests/`        | pytest suite                                               |
| `docs/`         | user, integration, and engineering documentation           |
| `integrations/` | host adapter configs and install/verify scripts            |
| `frontend/`     | optional React + Vite visualization stack                  |
