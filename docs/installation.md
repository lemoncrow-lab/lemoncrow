# Installation & Configuration

## Requirements

| Requirement | Version | Notes                                                                                    |
| ----------- | ------- | ---------------------------------------------------------------------------------------- |
| Python      | 3.12+   | Required                                                                                 |
| uv          | latest  | Package manager (`pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh \| sh`) |
| SQLite      | 3.35+   | Bundled with Python; FTS5 required                                                       |
| PostgreSQL  | 15+     | Optional, production-scale                                                               |
| pgvector    | 0.5+    | Optional, for embedding-based block search                                               |

## Install

```bash
cd atelier
uv sync --all-extras
```

Verify:

```bash
uv run atelier --version
uv run atelier-mcp --version
```

## Initialize the Store

```bash
uv run atelier init
# or with explicit root:
uv run atelier --root /path/to/.atelier init
```

Creates:

```
.atelier/
├── atelier.db          # SQLite store (blocks, traces, rubrics)
├── blocks/             # Markdown mirrors of ReasonBlocks
│   ├── *.md            # 10 pre-seeded blocks
├── rubrics/            # YAML mirrors of rubrics
│   ├── *.yaml          # 5 pre-seeded rubrics
└── traces/             # JSON mirrors of traces (written on record)
```

## Storage Backends

### SQLite (default, zero-config)

No additional setup needed. The database is created at `$ATELIER_ROOT/atelier.db`.

```bash
# Use default root (.atelier/ relative to cwd)
uv run atelier init

# Use custom root
uv run atelier --root ~/.my-atelier init
# or
ATELIER_ROOT=~/.my-atelier uv run atelier init
```

### PostgreSQL (optional)

For shared/production use or when you have many agents writing traces concurrently:

```bash
ATELIER_STORAGE_BACKEND=postgres \
ATELIER_DATABASE_URL=postgresql://user:pass@localhost:5432/atelier \
uv run atelier init
```

### pgvector (optional boost)

Enable embedding-based similarity search alongside FTS5 (additive, not a replacement):

```bash
ATELIER_STORAGE_BACKEND=postgres \
ATELIER_DATABASE_URL=postgresql://... \
ATELIER_VECTOR_SEARCH_ENABLED=true \
ATELIER_EMBEDDING_MODEL=text-embedding-3-small \
uv run atelier init
```

## Environment Variables

All configuration is via environment variables. No config file is required.

### Core

| Variable                 | Default                      | Description                                          |
| ------------------------ | ---------------------------- | ---------------------------------------------------- |
| `ATELIER_ROOT`           | `~/.atelier`                 | Trace/history store root directory                   |
| `ATELIER_KNOWLEDGE_ROOT` | `$WORKSPACE_ROOT/.knowledge` | Git-tracked ReasonBlocks and rubrics directory       |
| `ATELIER_WORKSPACE_ROOT` | `.`                          | Workspace root used by MCP server for relative paths |
| `ATELIER_STORE_ROOT`     | `~/.atelier`                 | Alias to override `ATELIER_ROOT`                     |

### Storage

| Variable                        | Default                  | Description                                     |
| ------------------------------- | ------------------------ | ----------------------------------------------- |
| `ATELIER_STORAGE_BACKEND`       | `sqlite`                 | `sqlite` or `postgres`                          |
| `ATELIER_DATABASE_URL`          | `""`                     | PostgreSQL DSN (required when backend=postgres) |
| `ATELIER_VECTOR_SEARCH_ENABLED` | `false`                  | Enable pgvector similarity search               |
| `ATELIER_EMBEDDING_DIM`         | `1536`                   | Embedding dimension (match your model)          |
| `ATELIER_EMBEDDING_MODEL`       | `text-embedding-3-small` | Model name for embedding generation             |

### HTTP Service

| Variable                  | Default     | Description                                                                      |
| ------------------------- | ----------- | -------------------------------------------------------------------------------- |
| `ATELIER_SERVICE_ENABLED` | `false`     | Enable the HTTP service                                                          |
| `ATELIER_SERVICE_HOST`    | `127.0.0.1` | Service bind host                                                                |
| `ATELIER_SERVICE_PORT`    | `8787`      | Service port                                                                     |
| `ATELIER_REQUIRE_AUTH`    | `true`      | Require `Authorization: Bearer <key>` header                                     |
| `ATELIER_API_KEY`         | `""`        | API key (set to empty string to allow no-key dev access when REQUIRE_AUTH=false) |

### MCP Server

| Variable              | Default                 | Description                                                      |
| --------------------- | ----------------------- | ---------------------------------------------------------------- |
| `ATELIER_MCP_MODE`    | `local`                 | `local` (in-process store) or `remote` (forward to HTTP service) |
| `ATELIER_SERVICE_URL` | `http://localhost:8787` | Remote service URL (when MCP_MODE=remote)                        |

### Integrations

| Variable                             | Default      | Description                                      |
| ------------------------------------ | ------------ | ------------------------------------------------ |
| `ATELIER_OPENMEMORY_ENABLED`         | `false`      | Enable OpenMemory bridge (no-op stub by default) |
| `ATELIER_OPENMEMORY_MCP_SERVER_NAME` | `openmemory` | MCP server name for OpenMemory                   |

### Cost tracking

| Variable                    | Default | Description                              |
| --------------------------- | ------- | ---------------------------------------- |
| `ATELIER_USD_PER_1K_TOKENS` | `0.003` | Token cost estimate for savings tracking |

## Verify Installation

```bash
cd atelier
make verify
```

Expected: ruff ✓, black --check ✓, mypy ✓, pytest 209 passed / 9 skipped

The 9 skipped tests are Postgres-gated (require `ATELIER_DATABASE_URL`). Skips are expected in a default install.

## Per-Agent Host Setup

After installing Atelier, wire it into your agent host:

- **Claude Code**: [docs/hosts/claude-code.md](hosts/claude-code.md)
- **Codex**: [docs/hosts/codex.md](hosts/codex.md)
- **VS Code Copilot**: [docs/hosts/copilot.md](hosts/copilot.md)
- **opencode**: [docs/hosts/opencode.md](hosts/opencode.md)
- **Gemini CLI**: [docs/hosts/gemini-cli.md](hosts/gemini-cli.md)
