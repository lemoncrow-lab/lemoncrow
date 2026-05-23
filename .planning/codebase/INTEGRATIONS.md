# External Integrations

**Analysis Date:** 2026-05-23

## APIs & External Services

**LLM Providers:**
- OpenAI-compatible Chat/Embeddings - Internal background LLM + embedding calls
  - SDK/Client: `openai` package and raw HTTPS fallback (`src/atelier/infra/internal_llm/openai_client.py`, `src/atelier/infra/embeddings/openai_embedder.py`, `src/atelier/infra/storage/vector.py`)
  - Auth: `ATELIER_OPENAI_API_KEY` / `OPENAI_API_KEY` (`src/atelier/infra/internal_llm/openai_client.py`)
- Ollama - Local LLM backend for summarization/chat
  - SDK/Client: `ollama` package (`src/atelier/infra/internal_llm/ollama_client.py`)
  - Auth: Not applicable (local server usage)

**Memory Sidecars:**
- Letta sidecar - Optional external memory backend and embedding delegate
  - SDK/Client: `letta_client` (`src/atelier/infra/memory_bridges/letta_adapter.py`, `src/atelier/infra/embeddings/letta_embedder.py`)
  - Auth: `ATELIER_LETTA_API_KEY` (and URL via `ATELIER_LETTA_URL`) (`src/atelier/infra/memory_bridges/letta_adapter.py`, `deploy/letta/docker-compose.yml`)
- OpenMemory sidecar - MCP-over-HTTP memory bridge with REST fallback
  - SDK/Client: stdlib `urllib` OpenMemory client (`src/atelier/gateway/integrations/openmemory.py`, `src/atelier/infra/memory_bridges/openmemory.py`)
  - Auth: `OPENAI_API_KEY` or `ATELIER_OPENMEMORY_OPENAI_API_KEY` required by setup flow (`src/atelier/gateway/adapters/cli.py`)

**Agent Host Integrations (via MCP):**
- Claude Code, Codex CLI, Copilot, opencode, Antigravity - Host-specific installer/config surfaces
  - SDK/Client: MCP server command `atelier-mcp` (`integrations/README.md`, `src/atelier/gateway/hosts/configs/*.yaml`, `src/atelier/gateway/adapters/mcp_server.py`)
  - Auth: host-specific CLI/API keys detected per host config (`src/atelier/gateway/hosts/configs/claude.yaml`, `codex.yaml`, `copilot.yaml`, `opencode.yaml`, `antigravity.yaml`)

## Data Storage

**Databases:**
- SQLite (default runtime store)
  - Connection: local filesystem path via `ATELIER_ROOT` (`src/atelier/core/service/config.py`, `src/atelier/infra/storage/sqlite_store.py`)
  - Client: built-in `sqlite3` and `SQLiteStore` (`src/atelier/infra/storage/sqlite_store.py`)
- PostgreSQL (optional backend)
  - Connection: `ATELIER_DATABASE_URL` (`src/atelier/core/service/config.py`, `src/atelier/infra/storage/factory.py`)
  - Client: `psycopg` via `PostgresStore` (`src/atelier/infra/storage/postgres_store.py`)

**File Storage:**
- Local filesystem runtime store under `.atelier`/`ATELIER_ROOT` (`README.md`, `docs/installation.md`, `src/atelier/core/service/config.py`)

**Caching:**
- SQLite-backed vector cache (`vector_cache.sqlite`) for embeddings (`src/atelier/infra/storage/vector.py`)

## Authentication & Identity

**Auth Provider:**
- Custom Bearer token auth for service API
  - Implementation: FastAPI dependency `verify_api_key`, controlled by `ATELIER_REQUIRE_AUTH` + `ATELIER_API_KEY` (`src/atelier/core/service/auth.py`, `src/atelier/core/service/config.py`)

## Monitoring & Observability

**Error Tracking:**
- Not detected as a dedicated error-tracking SaaS

**Logs:**
- Local-first telemetry event store (`src/atelier/core/service/telemetry/local_store.py`, `src/atelier/core/service/telemetry/emit.py`)
- Optional remote OTLP export to PostHog ingest endpoint (`src/atelier/core/service/telemetry/config.py`, `src/atelier/core/service/telemetry/exporters/otel.py`, `deploy/otel-collector.yaml`)
- Frontend telemetry with `posthog-js` when enabled (`frontend/src/lib/telemetry.ts`, `frontend/src/lib/insightsApi.ts`)

## CI/CD & Deployment

**Hosting:**
- Local/native service + frontend stack (`Makefile`, `frontend/README.md`)
- Containerized service/frontend via Dockerfiles + Docker Compose (`Dockerfile.api`, `Dockerfile.frontend`, `docker-compose.yml`)
- Optional Letta sidecar container (`deploy/letta/docker-compose.yml`)

**CI Pipeline:**
- GitHub Actions for lint/typecheck/tests/security/docs governance (`.github/workflows/tests.yml`, `.github/workflows/docs-governance.yml`)

## Environment Configuration

**Required env vars:**
- Core service/runtime: `ATELIER_ROOT`, `ATELIER_SERVICE_HOST`, `ATELIER_SERVICE_PORT` (`docs/installation.md`, `src/atelier/core/service/config.py`)
- Auth: `ATELIER_REQUIRE_AUTH`, `ATELIER_API_KEY` (`docs/installation.md`, `src/atelier/core/service/auth.py`)
- Remote MCP mode: `ATELIER_SERVICE_URL` (`docs/installation.md`, `src/atelier/gateway/adapters/remote_client.py`)
- Storage: `ATELIER_STORAGE_BACKEND`, `ATELIER_DATABASE_URL` (`docs/installation.md`, `src/atelier/infra/storage/factory.py`)
- Telemetry: `ATELIER_TELEMETRY`, `ATELIER_OTEL_ENDPOINT`, `ATELIER_POSTHOG_KEY`, `ATELIER_POSTHOG_HOST` (`src/atelier/core/service/telemetry/config.py`)
- Sidecars: `ATELIER_LETTA_URL`, `ATELIER_LETTA_API_KEY`, `ATELIER_OPENMEMORY_URL` (`src/atelier/infra/memory_bridges/letta_adapter.py`, `src/atelier/gateway/integrations/openmemory.py`)
- Frontend runtime: `VITE_API_URL` (`frontend/vite.config.ts`, `docker-compose.yml`)

**Secrets location:**
- Environment variables and local env files (for example `.env.production.example` template present); secret values are not stored in repo docs (`docs/installation.md`, `.env.production.example`)

## Webhooks & Callbacks

**Incoming:**
- Not detected for external webhook receivers in service routes (`src/atelier/core/service/api.py`)

**Outgoing:**
- OTLP HTTP export callbacks to configured endpoint (commonly PostHog ingest) (`src/atelier/core/service/telemetry/exporters/otel.py`, `deploy/otel-collector.yaml`)
- OpenMemory/Letta HTTP client calls for sidecar interactions (`src/atelier/gateway/integrations/openmemory.py`, `src/atelier/infra/memory_bridges/letta_adapter.py`)

---

*Integration audit: 2026-05-23*
