# Technology Stack

**Analysis Date:** 2026-05-28

## Languages

**Primary:**
- Python 3.11+ — backend runtime, all core logic under `src/atelier/`
- TypeScript 5.5.3 — frontend dashboard under `frontend/`

**Secondary:**
- YAML — config files, rubrics, seed blocks (`src/atelier/core/rubrics/`, `src/atelier/infra/seed_blocks/`)
- TOML — project config (`pyproject.toml`), telemetry config (`~/.config/atelier/telemetry.toml`)

## Runtime

**Backend Environment:**
- Python 3.12-slim (Docker production image, `Dockerfile.api`)
- Python 3.11 minimum (enforced by `requires-python = ">=3.11"` in `pyproject.toml`)

**Frontend Environment:**
- Bun 1.x — dev server and package manager (`docker-compose.yml`)
- Node.js 24.x — available on host (detected: v24.12.0)
- Nginx 1.27-alpine — production static file serving (`Dockerfile.frontend`)

**Package Manager (Backend):**
- `uv` 0.11.7 — dependency resolution and venv management
- Lockfile: `uv.lock` (present, frozen installs via `uv sync --frozen`)

**Package Manager (Frontend):**
- `bun` 1.3.13 — install and run scripts
- Lockfile: implicit via `bun install --frozen-lockfile`

## Frameworks

**Core Backend:**
- FastAPI >=0.136.1 — HTTP service API (`src/atelier/core/service/api.py`, `src/atelier/gateway/adapters/http_api.py`)
- uvicorn[standard] >=0.46.0 — ASGI server, launched via `atelier service start`
- Click >=8.1 — CLI framework, entry point `src/atelier/gateway/cli/app.py`
- Pydantic >=2.6 — data models and validation throughout `src/atelier/core/foundation/`
- pydantic-settings >=2.14.0 — environment-driven configuration (`src/atelier/core/service/config.py`)

**Core Frontend:**
- React 18.3.1 — UI component framework (`frontend/src/`)
- React Router DOM 6.26.0 — SPA routing
- TailwindCSS 3.4.6 — utility-first CSS
- Vite 5.4.21 — build tool and dev server (`vite --host 0.0.0.0 --port 3125`)

**Testing:**
- pytest >=9.0.3 — test runner, config in `pyproject.toml` `[tool.pytest.ini_options]`
- pytest-xdist >=3.8.0 — parallel test execution (`-n auto --dist=loadfile`)
- pytest-cov >=5.0 — coverage (`--cov=atelier`)
- vitest >=2.1.5 — frontend unit test runner, config in `frontend/scripts/run-vitest.mjs`
- @testing-library/react 16.1.0 — React component testing

**Build / Dev:**
- hatchling — Python wheel builder (`[build-system]` in `pyproject.toml`)
- ruff >=0.5 — linting (`line-length = 100`, target `py311`)
- black >=24.4 — code formatting (`line-length = 120`)
- mypy >=1.20.2 — static type checking (strict mode)
- TypeScript compiler (`tsc -b`) — frontend type checking

## Key Dependencies

**Critical Backend:**
- `litellm >=1.83.14` — multi-LLM provider routing and token cost pricing (`src/atelier/core/capabilities/pricing.py`)
- `tiktoken >=0.9` — token counting for context management
- `SQLAlchemy >=2.0.49` — ORM used with optional PostgreSQL backend
- `opentelemetry-api/sdk/exporter-otlp-proto-http >=1.27` — distributed tracing export
- `prometheus-client >=0.21` — Prometheus metrics endpoint
- `tenacity >=9.0` — retry logic for external calls
- `pybreaker >=1.2` — circuit breaker pattern
- `river >=0.22` — online/incremental machine learning (model routing calibration)
- `networkx >=3.4` — graph analysis (repo map, code dependency graphs)
- `ortools >=9.10` — Google OR-Tools, constraint/budget optimization (`src/atelier/core/capabilities/budget_optimizer/`)
- `tree-sitter >=0.23` + `tree-sitter-language-pack >=1.8.1` — multi-language AST parsing (`src/atelier/infra/tree_sitter/`)
- `GitPython >=3.1.50` — Git operations (high-level)
- `pygit2 ==1.19.2` — Git history, blame, renames (`src/atelier/infra/code_intel/git_history/`)
- `blake3 >=0.4.1` — fast cryptographic hashing for content addressing
- `datasketch >=1.6` — probabilistic deduplication (MinHash, LSH)
- `diff-match-patch >=2.1` — text diffing for patch application
- `pexpect >=4.9.0` — subprocess/PTY interaction

**Critical Frontend:**
- `posthog-js ^1.150.0` — product analytics in browser (`frontend/src/`)
- `react-markdown ^10.1.0` — markdown rendering in UI
- `lucide-react ^1.16.0` — icon library

**Optional Extras (install with `pip install atelier[extra]`):**
| Extra | Key Package | Purpose |
|-------|-------------|---------|
| `mcp` | `mcp >=1.0` | MCP protocol server (`src/atelier/gateway/adapters/mcp_server.py`) |
| `memory` | `letta-client >=1.7.12` | Letta memory sidecar client |
| `memory-server` | `letta >=0.16.7` | Full Letta server |
| `cloud` | `openai >=1.0` | OpenAI/compatible LLM calls |
| `postgres` | `psycopg[binary] >=3.1` | PostgreSQL storage backend |
| `vector` | `pgvector >=0.2`, `numpy >=1.26` | Vector similarity in Postgres |
| `repo-map` | `tree-sitter`, `networkx` | Repository structure maps |
| `rename` | `rope >=0.23` | Python symbol renaming |
| `smart` | `ollama >=0.4.0` | Ollama local LLM |
| `telemetry` | OpenTelemetry packages | OTel tracing |

## Configuration

**Environment Variables (primary config mechanism):**
- `ATELIER_ROOT` — root data directory (default: `~/.atelier`)
- `ATELIER_WORKSPACE_ROOT` — active project root
- `ATELIER_LESSONS_ROOT` — per-project lessons (default: `<workspace>/.lessons`)
- `ATELIER_STORAGE_BACKEND` — `sqlite` (default) or `postgres`
- `ATELIER_DATABASE_URL` — PostgreSQL connection string (e.g. `postgresql://user:pass@host/db`)
- `ATELIER_SERVICE_HOST` — service bind host (default: `127.0.0.1`)
- `ATELIER_SERVICE_PORT` — service port (default: `8787`)
- `ATELIER_SERVICE_URL` — remote service URL for MCP remote mode
- `ATELIER_API_KEY` — bearer token for service auth
- `ATELIER_REQUIRE_AUTH` — enable/disable auth (default: `false`)
- `ATELIER_MCP_MODE` — `local` or `remote`
- `ATELIER_AGENT` — which agent host is active
- `ATELIER_MODEL` — active LLM model identifier
- `ATELIER_LLM_BACKEND` — `ollama` (default) or `openai`
- `ATELIER_OPENAI_API_KEY` — OpenAI API key (falls back to `OPENAI_API_KEY`)
- `ATELIER_OPENAI_BASE_URL` — OpenAI-compatible endpoint base URL
- `ATELIER_OPENAI_MODEL` — model name (default: `gpt-4o-mini`)
- `ATELIER_EMBEDDER` — `local`, `openai`, `letta`, or `null`
- `ATELIER_TELEMETRY` — `1`/`true` to enable or `0`/`false` to disable
- `ATELIER_OTEL_ENDPOINT` — OTLP HTTP endpoint (default: `http://localhost:4318`)
- `ATELIER_POSTHOG_KEY` — PostHog project API key
- `ATELIER_POSTHOG_HOST` — PostHog host (default: `https://us.i.posthog.com`)
- `ATELIER_LETTA_URL` — Letta sidecar URL (default: `http://localhost:8283`)
- `ATELIER_LETTA_API_KEY` — Letta API key
- `ATELIER_OPENMEMORY_URL` — OpenMemory API URL (default: `http://127.0.0.1:8765`)
- `ATELIER_LANGFUSE_ENABLED` — `true`/`1` to enable Langfuse tracing
- `ATELIER_DEV_MODE` — enables gated features (lint, reasoning, verify)
- `ATELIER_CACHE_DISABLED` — `1` to disable caching
- `GITHUB_TOKEN` — GitHub API token for lesson PR bot
- `VITE_API_URL` — frontend env pointing to backend (default: `http://atelier-service:8787`)

**Build:**
- `pyproject.toml` — Python project, build, lint, test, type-check config
- `frontend/package.json` — frontend scripts and dependencies
- `Dockerfile.api` — API container (python:3.12-slim + uv)
- `Dockerfile.frontend` — frontend container (bun build → nginx serve)
- `docker-compose.yml` — orchestrates service + frontend containers
- `deploy/otel-collector.yaml` — OpenTelemetry Collector configuration

## Platform Requirements

**Development:**
- Python >=3.11 (3.12 recommended per Dockerfile)
- uv >=0.11 installed globally
- Bun 1.x for frontend
- Optional: Docker + Docker Compose for containerized dev

**Production:**
- Docker containerized deployment (API on port 8787, frontend on port 3125)
- Persistent volume at `~/.atelier` (or `ATELIER_ROOT`)
- Optional: PostgreSQL database (for `postgres` storage backend)
- Optional: OpenTelemetry Collector sidecar (for telemetry routing)

---

*Stack analysis: 2026-05-28*
