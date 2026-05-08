# Atelier — Open-Source Reasoning Runtime

**Reusable engineering judgment for AI-assisted coding.**

Atelier helps make your best engineers’ judgment available to junior engineers and AI agents.

Atelier sits between agent hosts and their environments, providing:

- **Reasoning reuse** — retrieve and inject known procedures (ReasonBlocks) into agent context before runs
- **Semantic memory** — FTS + optional vector search over procedures and traces
- **Loop detection** — monitor for thrashing, second-guessing, and budget exhaustion
- **Tool supervision** — cached reads, memoized searches, injection-guarded grep
- **Context compression** — ledger summarisation for long-running tasks
- **Rubric verification** — gate agent plans and outputs against domain-specific rubrics
- **Failure rescue** — record observable execution traces, detect recurring failures, surface targeted rescue procedures

> **Example:**
> Agent plan: "Parse Shopify product handle from URL."
> Atelier: `status: blocked` — "Known dead end. Use Product GID. Required: re-fetch by GID + post-publish audit."

## What Atelier is not

- **Not a general memory platform** — Atelier owns procedural memory (ReasonBlocks). Agent memory goes through `memory` to the configured backend: Letta, OpenMemory, Mem0, or local SQLite for development.
- **Not an agent framework** — Atelier does not execute tools, manage model calls, or own the agent loop.
- **Not an IDE** — Atelier runs as a sidecar to your agent host, not as a standalone coding environment.
- **Not a vector database** — FTS5 is the default retrieval; pgvector is optional for semantic similarity.

## Architecture

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

## Capability Model

- Reasoning reuse: Atelier augmentation, MCP `reasoning`, CLI `reasoning`
- Plan verification: Atelier augmentation, MCP `lint`, CLI `lint`
- Failure rescue: Atelier augmentation, MCP `rescue`, CLI `rescue`
- Weekly governance report: CLI `report`
- Style-guide import: CLI `import-style-guide` into the human-reviewed lesson inbox
- Starter ReasonBlock packs: CLI `init --stack` and `init --list-stacks`
- Rubric verification: Atelier augmentation, MCP `verify`, CLI `verify`
- Trace recording: Atelier augmentation, MCP `trace`, CLI `trace record`
- Compact lifecycle: Atelier augmentation, MCP `compact` with `op="output"`, `op="session"`, or `op="advise"`
- Smart read/search/edit: Atelier augmentation, MCP `read`, `search`, `edit`; CLI `read`, `search`, `edit`
- Agent memory: Atelier augmentation, MCP `memory` with `op="block_upsert"`, `op="block_get"`, `op="archive"`, `op="recall"`; CLI `memory upsert/get/list/recall/archive`
- Lesson promotion: CLI `lesson inbox` and `lesson decide`
- Consolidation review: CLI `consolidation inbox` and `consolidation decide`
- Quality-aware routing: Atelier augmentation, MCP `route` with `op="decide"` or `op="verify"`

## Installation

**Requirements:** Python 3.12+, `uv`

```bash
cd atelier
uv sync --all-extras
uv run atelier init   # creates .atelier/ and seeds 10 ReasonBlocks + 5 rubrics
uv run atelier init --stack python-fastapi   # optional starter ReasonBlock templates
```

**Install into supported agent CLIs:**

```bash
make install   # deps + every supported CLI found on PATH + runtime init
make verify    # code checks + runtime smoke tests + host integration verification
```

For per-host or dry-run installs, use the scripts directly:

```bash
bash scripts/install_claude.sh --dry-run
bash scripts/install_codex.sh --print-only
```

**One-line bootstrap install (Linux/macOS):**

```bash
curl -fsSL https://raw.githubusercontent.com/leanchain/atelier/main/scripts/install.sh | bash
```

This installs/updates Atelier under `~/.local/share/atelier`, installs host integrations, initializes the runtime store, starts the background `servicectl` loop, and adds global wrappers in `~/.local/bin` (`atelier`, `atelier-mcp`, `atelier-status`).

→ Full install guide: [docs/installation.md](docs/installation.md)
→ Per-host guides: [docs/hosts/all-agent-clis.md](docs/hosts/all-agent-clis.md)
→ V2 to V3 migration guide: [docs/migrations/v2-to-v3.md](docs/migrations/v2-to-v3.md)

## Quickstart

```bash
# 1. Check a plan before executing it
uv run atelier lint \
    --task "Publish Shopify product" \
    --domain Agent.shopify.publish \
    --step "Parse product handle from PDP URL" \
    --step "Use handle to update metafields"
# → status: blocked (exit 2) — dead end detected

# 2. Get reasoning context
uv run atelier reasoning \
    --task "Fix Shopify JSON-LD availability" \
    --domain Agent.pdp.schema \
    --file pdp/schema.py

# 3. Run a rubric gate
echo '{"product_identity_uses_gid": true, "pre_publish_snapshot_exists": true, "write_result_checked": true}' \
  | uv run atelier verify rubric_shopify_publish
```

→ Full tutorial: [docs/quickstart.md](docs/quickstart.md)

## Runtime Modes

The default install is **CLI + MCP + background processing**.

- `atelier ...` works with no HTTP server.
- `atelier-mcp` works with no HTTP server.
- `atelier servicectl ...` manages the detached offline processing loop.
- `atelier stack ...` is optional and only needed when you want the UI/service visualization stack.

Pure CLI mode still emits Atelier telemetry events unless disabled. Turn it off with `atelier telemetry off` or `ATELIER_TELEMETRY=0`.

## Start the Optional UI Stack

Run the API service and React dashboard with the installed stack command:

```bash
atelier stack start
```

Then open the frontend at [http://localhost:3125](http://localhost:3125).
The API service runs at [http://localhost:8787](http://localhost:8787).

For development inside the repo, `make start` still launches the same Compose stack.

## Background Processing

The installer starts a detached background controller by default. It periodically enqueues and processes offline jobs, and it works on the default SQLite install as well as Postgres.

```bash
atelier servicectl status
atelier servicectl logs
atelier servicectl stop
atelier servicectl start
```

Queue work can also be driven manually:

```bash
atelier worker enqueue consolidate_reasonblocks
atelier worker run-once
atelier worker list
```

## CLI

```bash
uv run atelier [--root PATH] COMMAND [OPTIONS]
```

| Command                                 | Description                               |
| --------------------------------------- | ----------------------------------------- |
| `init`                                  | Create store and seed blocks/rubrics      |
| `reasoning`                             | Get reasoning context for a task          |
| `lint`                                  | Validate a plan (exit 2 = blocked)        |
| `rescue`                                | Suggest rescue for a failure              |
| `trace record/list/show`                | Record and browse execution traces        |
| `verify`                                | Run a rubric gate                         |
| `block list/add/extract`                | Manage ReasonBlocks                       |
| `rubric list/show/add`                  | Manage rubrics                            |
| `env list/show`                         | List reasoning environments               |
| `failure list/show/accept`              | Manage failure clusters                   |
| `ledger list/show`                      | Browse run ledger                         |
| `lesson inbox/decide`                   | Review generated lesson candidates        |
| `consolidation inbox/decide`            | Review memory consolidation candidates    |
| `report`                                | Generate governance reports               |
| `proof run/show`                        | Run or display proof report output        |
| `route`                                 | Quality-aware routing decisions           |
| `memory upsert/get/list/recall/archive` | Session memory block operations           |
| `search`                                | Semantic retrieval across ReasonBlocks    |
| `read`                                  | AST-aware file read with symbol summary   |
| `edit`                                  | Batch find/replace edits from JSON file   |
| `bench runtime`                         | Capability efficiency metrics             |
| `service`                               | Start/configure the optional HTTP service |
| `worker`                                | Enqueue and run background jobs           |
| `servicectl`                            | Manage the detached offline processor     |
| `stack`                                 | Start/stop the optional UI stack          |

All commands accept `--json` for machine-readable output.

→ Full reference: [docs/cli.md](docs/cli.md)

## MCP Server

```bash
uv run atelier-mcp
```

Stdio JSON-RPC server. The agent-facing MCP surface is deliberately small:

`reasoning`, `lint`, `route`,
`rescue`, `trace`, `verify`,
`memory`, `read`, `edit`, `search`,
`compact`, `atelier_repo_map`.

Governance, admin, benchmarks, read-only SQL inspection, and static routing contracts live on the CLI.

→ Full reference: [docs/engineering/mcp.md](docs/engineering/mcp.md)

## Host Integrations

Atelier runs the same runtime across hosts, but integration and enforcement are host-native per CLI
surface rather than a single identical plugin model.

| Host                | Interface             | Status       | Install guide                       |
| ------------------- | --------------------- | ------------ | ----------------------------------- |
| **Claude Code**     | MCP + skills + agents | ✅ Supported | `docs/hosts/claude-code-install.md` |
| **Codex CLI**       | MCP + AGENTS.md       | ✅ Supported | `docs/hosts/codex-install.md`       |
| **VS Code Copilot** | MCP + instructions    | ✅ Supported | `docs/hosts/copilot-install.md`     |
| **opencode**        | MCP                   | ✅ Supported | `docs/hosts/opencode-install.md`    |
| **Gemini CLI**      | MCP                   | ✅ Supported | `docs/hosts/gemini-cli-install.md`  |

All installers: idempotent, back up before writing, skip gracefully if CLI is not on PATH.
Support `--dry-run`, `--print-only`, `--strict`. Never write secrets.

### Claude Code Plugin Example

The Claude Code integration shows Atelier status, active model, cost estimate, and MCP health in
the terminal status line.

![Claude Code plugin example showing Atelier in the terminal status line](docs/assets/claude-plugin-example.png)

→ Details: [docs/hosts/all-agent-clis.md](docs/hosts/all-agent-clis.md)

## Python SDK

```python
from atelier.sdk import AtelierClient

client = AtelierClient.local(root=".atelier")

context = client.get_reasoning_context(
    task="Publish Shopify product",
    domain="Agent.shopify.publish",
)

check = client.check_plan(
    task="Publish Shopify product",
    domain="Agent.shopify.publish",
    plan=["Parse product handle from PDP URL"],
)

if check.status == "blocked":
    rescue = client.rescue_failure(
        task="Publish Shopify product",
        error="Known dead end triggered",
    )

result = client.run_rubric_gate(
    rubric_id="rubric_shopify_publish",
    checks={"product_identity_uses_gid": True},
)
```

Available clients: `AtelierClient`, `LocalClient`, `RemoteClient`, `MCPClient`,
`ReasonBlockClient`, `RubricClient`, `TraceClient`, `EvalClient`, `SavingsClient`

→ Reference: [docs/sdk/python.md](docs/sdk/python.md)

## Storage

| Path                      | Contents                                                 |
| ------------------------- | -------------------------------------------------------- |
| `.atelier/atelier.db`     | SQLite + FTS5 — all blocks, traces, rubrics              |
| `.atelier/blocks/*.md`    | Markdown mirror of every ReasonBlock (reviewable in PRs) |
| `.atelier/traces/*.json`  | JSON mirror of every recorded trace                      |
| `.atelier/rubrics/*.yaml` | YAML mirror of every rubric                              |

Key environment variables:

| Variable                  | Default                 | Description                        |
| ------------------------- | ----------------------- | ---------------------------------- |
| `ATELIER_ROOT`            | `.atelier`              | Store root directory               |
| `ATELIER_STORAGE_BACKEND` | `sqlite`                | `sqlite` or `postgres`             |
| `ATELIER_DATABASE_URL`    | `""`                    | PostgreSQL DSN (if using postgres) |
| `ATELIER_MCP_MODE`        | `local`                 | `local` or `remote`                |
| `ATELIER_SERVICE_URL`     | `http://localhost:8787` | Remote service URL                 |
| `ATELIER_API_KEY`         | `""`                    | API key for remote service         |
| `ATELIER_SERVICE_ENABLED` | `false`                 | Enable HTTP service                |
| `ATELIER_REQUIRE_AUTH`    | `true`                  | Require API key on HTTP service    |

→ Full variable reference: [docs/installation.md](docs/installation.md)

## HTTP Service (optional)

```bash
ATELIER_SERVICE_ENABLED=true ATELIER_REQUIRE_AUTH=false make service
# → http://localhost:8787
# → http://localhost:8787/docs (Swagger UI)
```

Endpoints: `/health`, `/ready`, `/metrics`, `/v1/reasoning/*`, `/v1/rubrics`, `/v1/traces`,
`/v1/reasonblocks`, `/v1/environments`, `/v1/evals`, `/v1/extract/*`, `/v1/failures/*`

→ Details: [docs/engineering/service.md](docs/engineering/service.md)

## Safety

- No chain-of-thought storage — only observable fields (commands, errors, diff summaries)
- Redaction filter applied to all trace fields before persistence
- No secret storage — `ATELIER_API_KEY` and tokens are never written to the store
- Hooks disabled by default — `integrations/claude/plugin/hooks/` requires explicit opt-in
- OpenMemory bridge is a no-op until `ATELIER_OPENMEMORY_ENABLED=true`
- Cached-grep injection guard — patterns validated before shell execution

→ Details: [docs/engineering/security.md](docs/engineering/security.md)

## Benchmarks

### Honest V3 Replay

The reproducible V3 benchmark replays 50 synthetic host transcripts and records deterministic token accounting for baseline host output versus Atelier-assisted output. The latest replay measured a 13.27% input-token reduction on the synthetic corpus.

```bash
make bench-savings-honest
```

→ Methodology and CSV: [docs/benchmarks/v3-honest-savings.md](docs/benchmarks/v3-honest-savings.md)

### Historical V2 Smoke Harness

The old deterministic V2 benchmark is retained only as a parser and trace-continuity smoke test. It is not used for public percentage claims.

```bash
uv run atelier --root /tmp/bench init
uv run atelier --root /tmp/bench benchmark --rounds 5 --model claude-sonnet-4.6 --json
uv run atelier --root /tmp/bench savings-detail
```

## Development

```bash
cd atelier
make install         # deps + host integrations + status helper + runtime init
make test            # pytest
make lint            # ruff check
make format-check    # black --check
make typecheck       # mypy --strict
make verify          # code checks + runtime smoke tests + host verification
make pre-commit      # format + lint + typecheck + tests
```

→ Dev guide: [docs/engineering/contributing.md](docs/engineering/contributing.md)

## Repository Layout

| Path            | Purpose                                                       |
| --------------- | ------------------------------------------------------------- |
| `src/atelier/`  | Core engine: models, store, runtime, CLI, MCP server, service |
| `tests/`        | pytest suite                                                  |
| `docs/`         | Documentation                                                 |
| `integrations/` | Host adapter configs and install/verify scripts               |
| `frontend/`     | React + Vite dashboard                                        |

## Docs Index

| Document                                         | For whom      | Content                                                |
| ------------------------------------------------ | ------------- | ------------------------------------------------------ |
| **[AGENT_README.md](AGENT_README.md)**           | Coding agents | Decision trees, workflows, JSON tool specs, hard rules |
| **[QUICK_REFERENCE.md](QUICK_REFERENCE.md)**     | Developers    | One-page cheat sheet: skills, agents, tools, commands  |
| **[docs/](docs/README.md)**                      | Everyone      | Full documentation index                               |
| **[docs/installation.md](docs/installation.md)** | New users     | Setup, backends, env vars                              |
| **[docs/quickstart.md](docs/quickstart.md)**     | New users     | 5-minute tutorial                                      |
| **[docs/engineering/](docs/engineering/)**       | Contributors  | Architecture, security, storage, service, MCP          |
| **[docs/hosts/](docs/hosts/)**                   | Integrators   | Per-host install, verify, uninstall, troubleshooting   |
