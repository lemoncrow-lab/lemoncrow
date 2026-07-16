# Installation & Configuration

This page starts with the installed product flow. Source-checkout and contributor setup are lower down.

## Quick Install (Production)

```bash
curl -fsSL https://install.lemoncrow.com | bash
```

What the production installer does:

- downloads a pre-compiled LemonCrow binary for your platform from the latest release
- installs it to `~/.local/bin/`
- adds the directory to `PATH` in your shell profile

The binary is self-contained — no `git`, `uv`, `npm`, or `node` required at install time.

## Full Developer Install

For host integrations, background services, and the optional
visualization stack, install from a repo checkout using the dev installer:

```bash
git clone https://github.com/lemoncrow-lab/lemoncrow.git
cd lemoncrow
bash scripts/local.sh --local
```

The dev installer:

- installs `lc` and `lc mcp` as user-level console commands in `~/.local/bin`
- clones or updates LemonCrow under `~/.local/share/lemoncrow`
- initializes `~/.lemoncrow`
- starts the detached `servicectl` loop
- attempts to start the optional visualization stack when npm is available
- installs host integrations when compatible CLIs are found on `PATH`

The dev installer uses uv at install time to create a managed tool environment.
After install, `lc` and `lc mcp` run directly from that environment;
normal CLI usage does not shell through `uv run`.

Verify the install:

```bash
lc --version
lc mcp --version
lc background status
```

## Useful Installer Variants

Skip host integrations:

```bash
curl -fsSL https://raw.githubusercontent.com/lemoncrow-lab/lemoncrow/main/scripts/local.sh | bash -s -- --no-hosts
```

Skip auto-starting background services:

```bash
curl -fsSL https://raw.githubusercontent.com/lemoncrow-lab/lemoncrow/main/scripts/local.sh | LEMONCROW_NO_SERVICECTL=1 bash
```

Skip auto-starting the visualization stack:

```bash
curl -fsSL https://raw.githubusercontent.com/lemoncrow-lab/lemoncrow/main/scripts/local.sh | LEMONCROW_NO_STACK=1 bash
```

Install from a local checkout instead of GitHub:

```bash
bash scripts/local.sh --local
```

Install host + universal MCP artifacts into the current project (instead of user-global host config):

```bash
bash scripts/local.sh --local --workspace .
```

## Runtime Modes After Install

### Default Runtime

No HTTP server is required for normal usage.

- `lc ...` is the main CLI
- `lc mcp` is the MCP server used by host integrations
- `lc background ...` manages background services and auto-updates

If npm is installed and `LEMONCROW_NO_STACK=1` was not set during install, the
installer will also register the visualization stack as a background service for you.

### Background Services & Auto-Update

LemonCrow uses your OS-native manager (**systemd** on Linux, **launchd** on macOS) to ensure background tasks and the visualization stack are always running.

```bash
# Check service health and auto-update status
lc background status

# View background logs
lc background logs controller
lc background logs stack

# Restart the entire stack (e.g. after a manual code change)
lc background restart
```

#### Auto-Update

The background controller periodically checks your git repository for updates. When found, it automatically:

1. Pulls the latest code.
2. Syncs dependencies using `uv`.
3. Restarts the services to apply changes.

### Optional UI Stack

Manage the visualization UI as a background service:

```bash
lc background restart  # Restarts both controller and stack
```

Or control the native stack manually:

```bash
lc stack start
```

Then open:

- `http://localhost:3125` for the frontend
- `http://localhost:8787` for the service API

Other stack commands:

```bash
lc stack status
lc stack logs
lc stack stop
```

### Optional HTTP Service Without the UI

If you want the service API without the full stack:

```bash
LEMONCROW_REQUIRE_AUTH=false lc service start --host 0.0.0.0 --port 8787
```

For authenticated deployments, set `LEMONCROW_API_KEY` and keep `LEMONCROW_REQUIRE_AUTH=true`.

### Background Controller Variables

The installer registers background services by default.

```bash
lc background status
lc background logs
```

Manual job control is available too:

```bash
lc worker enqueue consolidate_playbooks
lc worker run-once
lc worker list
```

### Installer Behavior Variables

| Variable                | Default | Description                                              |
| ----------------------- | ------- | -------------------------------------------------------- |
| `LEMONCROW_NO_HOSTS`      | `0`     | Skip host integration install scripts                    |
| `LEMONCROW_NO_SERVICECTL` | `0`     | Skip auto-registering background services during install |
| `LEMONCROW_NO_STACK`      | `0`     | Skip auto-registering the visualization stack service    |
| `LEMONCROW_LOCAL`         | `0`     | Install from the current checkout in editable mode       |

## Storage Backends

### SQLite (default)

SQLite is the default install mode and does not require any extra setup.

- store root: `~/.lemoncrow` by default
- queue-backed worker jobs are supported
- good default for local usage, single-user environments, and most host integrations

Store layout:

```text
.lemoncrow/
├── lemoncrow.db          # SQLite store (blocks, traces, rubrics, jobs)
├── blocks/             # Markdown mirrors of Playbooks
├── rubrics/            # YAML mirrors of rubrics
└── traces/             # JSON mirrors of recorded traces
```

### PostgreSQL (optional)

Use Postgres when you want shared storage, central deployment, or multi-writer operation.

```bash
LEMONCROW_STORAGE_BACKEND=postgres \
LEMONCROW_DATABASE_URL=postgresql://user:pass@localhost:5432/lemoncrow \
lc init
```

### pgvector (optional)

Embedding-based similarity search is optional and additive:

```bash
LEMONCROW_STORAGE_BACKEND=postgres \
LEMONCROW_DATABASE_URL=postgresql://... \
LEMONCROW_VECTOR_SEARCH_ENABLED=true \
LEMONCROW_EMBEDDING_MODEL=text-embedding-3-small \
lc init
```

## Environment Variables

### Core

| Variable               | Default            | Description                       |
| ---------------------- | ------------------ | --------------------------------- |
| `LEMONCROW_ROOT`         | `~/.lemoncrow`       | Main runtime store root           |
| `LEMONCROW_STORE_ROOT`   | `~/.lemoncrow`       | Alias for `LEMONCROW_ROOT`          |
| `LEMONCROW_LESSONS_ROOT` | workspace-relative | Optional git-tracked lessons root |

### Storage

| Variable                        | Default                  | Description                               |
| ------------------------------- | ------------------------ | ----------------------------------------- |
| `LEMONCROW_STORAGE_BACKEND`       | `sqlite`                 | `sqlite` or `postgres`                    |
| `LEMONCROW_DATABASE_URL`          | `""`                     | PostgreSQL DSN when backend is `postgres` |
| `LEMONCROW_VECTOR_SEARCH_ENABLED` | `false`                  | Enable pgvector similarity search         |
| `LEMONCROW_EMBEDDING_DIM`         | `1536`                   | Embedding dimension                       |
| `LEMONCROW_EMBEDDING_MODEL`       | `text-embedding-3-small` | Embedding model name                      |

### Background Controller

| Variable                                          | Default | Description                                    |
| ------------------------------------------------- | ------- | ---------------------------------------------- |
| `LEMONCROW_NO_SERVICECTL`                           | `0`     | Skip auto-starting `servicectl` during install |
| `LEMONCROW_SERVICECTL_INTERVAL_SECONDS`             | `60`    | Poll interval for the detached loop            |
| `LEMONCROW_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS` | `21600` | Periodic maintenance enqueue interval          |

### Optional Stack

| Variable           | Default | Description                           |
| ------------------ | ------- | ------------------------------------- |
| `LEMONCROW_NO_STACK` | `0`     | Skip auto-starting the optional stack |

### Optional HTTP Service

| Variable               | Default     | Description                                 |
| ---------------------- | ----------- | ------------------------------------------- |
| `LEMONCROW_SERVICE_HOST` | `127.0.0.1` | Service bind host                           |
| `LEMONCROW_SERVICE_PORT` | `8787`      | Service port                                |
| `LEMONCROW_REQUIRE_AUTH` | `false`     | Require Bearer auth                         |
| `LEMONCROW_API_KEY`      | `""`        | Bearer token for authenticated service mode |

### MCP

| Variable              | Default | Description                                                        |
| --------------------- | ------- | ------------------------------------------------------------------ |
| `LEMONCROW_SERVICE_URL` | unset   | Remote service URL; when set, core MCP calls route to this service |

### Telemetry

| Variable | Default | Description |
| --- | --- | --- |
| LEMONCROW_TELEMETRY | enabled | Disable with `0`, `false`, `off`, or `no` |

If you are developing LemonCrow itself instead of using the installed product:

```bash
cd lemoncrow
uv sync --all-extras
lc init
```

Contributor verification flow:

```bash
make verify
```

When working from multiple git worktrees, bootstrap each worktree once with:

```bash
make worktree-env
```

If `.env.worktree` is present, `make start` and `make restart` automatically load it so each worktree gets its own ports and `.lemoncrow-worktree` runtime root.

## Per-Agent Host Setup

After installation, use the host-specific guides if you want to inspect or customize integration:

- [hosts/claude-code-install.md](hosts/claude-code-install.md)
- [hosts/copilot-install.md](hosts/copilot-install.md)
- [hosts/codex-install.md](hosts/codex-install.md)
- [hosts/opencode-install.md](hosts/opencode-install.md)
- [hosts/antigravity-install.md](hosts/antigravity-install.md)

### Optional Zoekt Backend

Zoekt is not bootstrapped by default. LemonCrow uses its internal code index,
local embeddings, and ripgrep unless an existing Zoekt runtime is detected.

- `LEMONCROW_ZOEKT_MODE=off` (default): never probe or route to Zoekt.
- `LEMONCROW_ZOEKT_MODE=installed`: use Zoekt only when its binaries are already
  installed or provided through `LEMONCROW_ZOEKT_BIN`.
- `LEMONCROW_ZOEKT_MODE=managed`: allow LemonCrow to provision and run the pinned
  Zoekt container through Docker for large repositories.
