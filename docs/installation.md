# Installation & Configuration

This page starts with the installed product flow. Source-checkout and contributor setup are lower down.

## Recommended Install for End Users

```bash
curl -fsSL https://raw.githubusercontent.com/leanchain/atelier/main/scripts/install.sh | bash
```

What the installer does:

- installs `atelier` and `atelier-mcp` into `~/.local/bin`
- clones or updates Atelier under `~/.local/share/atelier`
- initializes `~/.atelier`
- starts the detached `servicectl` loop
- installs host integrations when compatible CLIs are found on `PATH`

Verify the install:

```bash
atelier --version
atelier-mcp --version
atelier servicectl status
```

## Useful Installer Variants

Skip host integrations:

```bash
curl -fsSL https://raw.githubusercontent.com/leanchain/atelier/main/scripts/install.sh | bash -s -- --no-hosts
```

Skip auto-starting the background controller:

```bash
curl -fsSL https://raw.githubusercontent.com/leanchain/atelier/main/scripts/install.sh | ATELIER_NO_SERVICECTL=1 bash
```

Install from a local checkout instead of GitHub:

```bash
bash scripts/install.sh --local
```

## Runtime Modes After Install

### Default Runtime

No HTTP server is required for normal usage.

- `atelier ...` is the main CLI
- `atelier-mcp` is the MCP server used by host integrations
- `atelier servicectl ...` manages offline/background work

### Optional UI Stack

Start the visualization UI only when you want it:

```bash
atelier stack start
```

Then open:

- `http://localhost:3125` for the frontend
- `http://localhost:8787` for the service API

Other stack commands:

```bash
atelier stack status
atelier stack logs
atelier stack stop
```

### Optional HTTP Service Without the UI

If you want the service API without the full stack:

```bash
ATELIER_REQUIRE_AUTH=false atelier service start --host 0.0.0.0 --port 8787
```

For authenticated deployments, set `ATELIER_API_KEY` and keep `ATELIER_REQUIRE_AUTH=true`.

### Background Controller Variables

The installer starts `servicectl` by default.

```bash
atelier servicectl status
atelier servicectl logs
atelier servicectl stop
atelier servicectl start
```

Manual job control is available too:

```bash
atelier worker enqueue consolidate_reasonblocks
atelier worker run-once
atelier worker list
```

## Storage Backends

### SQLite (default)

SQLite is the default install mode and does not require any extra setup.

- store root: `~/.atelier` by default
- queue-backed worker jobs are supported
- good default for local usage, single-user environments, and most host integrations

Store layout:

```text
.atelier/
├── atelier.db          # SQLite store (blocks, traces, rubrics, jobs)
├── blocks/             # Markdown mirrors of ReasonBlocks
├── rubrics/            # YAML mirrors of rubrics
└── traces/             # JSON mirrors of recorded traces
```

### PostgreSQL (optional)

Use Postgres when you want shared storage, central deployment, or multi-writer operation.

```bash
ATELIER_STORAGE_BACKEND=postgres \
ATELIER_DATABASE_URL=postgresql://user:pass@localhost:5432/atelier \
atelier init
```

### pgvector (optional)

Embedding-based similarity search is optional and additive:

```bash
ATELIER_STORAGE_BACKEND=postgres \
ATELIER_DATABASE_URL=postgresql://... \
ATELIER_VECTOR_SEARCH_ENABLED=true \
ATELIER_EMBEDDING_MODEL=text-embedding-3-small \
atelier init
```

## Environment Variables

### Core

| Variable                 | Default            | Description                         |
| ------------------------ | ------------------ | ----------------------------------- |
| `ATELIER_ROOT`           | `~/.atelier`       | Main runtime store root             |
| `ATELIER_STORE_ROOT`     | `~/.atelier`       | Alias for `ATELIER_ROOT`            |
| `ATELIER_KNOWLEDGE_ROOT` | workspace-relative | Optional git-tracked knowledge root |

### Storage

| Variable                        | Default                  | Description                               |
| ------------------------------- | ------------------------ | ----------------------------------------- |
| `ATELIER_STORAGE_BACKEND`       | `sqlite`                 | `sqlite` or `postgres`                    |
| `ATELIER_DATABASE_URL`          | `""`                     | PostgreSQL DSN when backend is `postgres` |
| `ATELIER_VECTOR_SEARCH_ENABLED` | `false`                  | Enable pgvector similarity search         |
| `ATELIER_EMBEDDING_DIM`         | `1536`                   | Embedding dimension                       |
| `ATELIER_EMBEDDING_MODEL`       | `text-embedding-3-small` | Embedding model name                      |

### Background Controller

| Variable                                          | Default | Description                                    |
| ------------------------------------------------- | ------- | ---------------------------------------------- |
| `ATELIER_NO_SERVICECTL`                           | `0`     | Skip auto-starting `servicectl` during install |
| `ATELIER_SERVICECTL_INTERVAL_SECONDS`             | `60`    | Poll interval for the detached loop            |
| `ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS` | `21600` | Periodic maintenance enqueue interval          |

### Optional HTTP Service

| Variable               | Default     | Description                                 |
| ---------------------- | ----------- | ------------------------------------------- |
| `ATELIER_SERVICE_HOST` | `127.0.0.1` | Service bind host                           |
| `ATELIER_SERVICE_PORT` | `8787`      | Service port                                |
| `ATELIER_REQUIRE_AUTH` | `true`      | Require Bearer auth                         |
| `ATELIER_API_KEY`      | `""`        | Bearer token for authenticated service mode |

### MCP

| Variable              | Default                 | Description                           |
| --------------------- | ----------------------- | ------------------------------------- |
| `ATELIER_MCP_MODE`    | `local`                 | `local` or `remote`                   |
| `ATELIER_SERVICE_URL` | `http://localhost:8787` | Remote service URL in MCP remote mode |

### Telemetry

| Variable                    | Default | Description                               |
| --------------------------- | ------- | ----------------------------------------- |
| `ATELIER_TELEMETRY`         | enabled | Disable with `0`, `false`, `off`, or `no` |
| `ATELIER_USD_PER_1K_TOKENS` | `0.003` | Token cost estimate for savings reporting |

## Source Checkout and Contributor Install

If you are developing Atelier itself instead of using the installed product:

```bash
cd atelier
uv sync --all-extras
uv run atelier init
```

Contributor verification flow:

```bash
make verify
```

## Per-Agent Host Setup

After installation, use the host-specific guides if you want to inspect or customize integration:

- [hosts/claude-code-install.md](hosts/claude-code-install.md)
- [hosts/copilot-install.md](hosts/copilot-install.md)
- [hosts/codex-install.md](hosts/codex-install.md)
- [hosts/opencode-install.md](hosts/opencode-install.md)
- [hosts/gemini-cli-install.md](hosts/gemini-cli-install.md)
