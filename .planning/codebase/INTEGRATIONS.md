# External Integrations

**Analysis Date:** 2026-05-18

## APIs & External Services

**LLM, Embedding, and Memory Services:**

- OpenAI embeddings API and OpenAI-compatible chat endpoints - optional cloud/backhaul model access for embeddings and background summarization in `src/atelier/infra/embeddings/openai_embedder.py`, `src/atelier/infra/storage/vector.py`, and `src/atelier/infra/internal_llm/openai_client.py`
  - SDK/Client: `openai`, with `httpx`/`urllib.request` fallbacks
  - Auth: `OPENAI_API_KEY` or `ATELIER_OPENAI_API_KEY`
- Ollama - optional local model server for internal summarization/chat in `src/atelier/infra/internal_llm/ollama_client.py`
  - SDK/Client: `ollama`
  - Auth: Not applicable
- Letta sidecar - optional memory + embedding sidecar used by memory store and embedder auto-detection in `src/atelier/infra/memory_bridges/letta_adapter.py`, `src/atelier/infra/embeddings/letta_embedder.py`, and `deploy/letta/docker-compose.yml`
  - SDK/Client: `letta-client`
  - Auth: `ATELIER_LETTA_API_KEY`
- OpenMemory MCP bridge - optional best-effort memory pointer/context sync layered on top of the local store in `src/atelier/gateway/integrations/openmemory.py` and `src/atelier/infra/memory_bridges/openmemory.py`
  - SDK/Client: `AtelierClient.mcp()` / internal MCP client
  - Auth: Not detected; controlled by `ATELIER_OPENMEMORY_MCP_SERVER_NAME`

**Observability and Product Telemetry:**

- PostHog - frontend analytics and backend OTLP destination in `frontend/src/lib/telemetry.ts`, `src/atelier/core/service/telemetry/config.py`, and `deploy/otel-collector.yaml`
  - SDK/Client: `posthog-js`, OTLP HTTP exporter
  - Auth: `ATELIER_POSTHOG_KEY` and `POSTHOG_PROJECT_API_KEY`
- OpenTelemetry collector - optional log export pipeline for backend product telemetry in `src/atelier/core/service/telemetry/` and `deploy/otel-collector.yaml`
  - SDK/Client: `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`
  - Auth: endpoint-driven via `ATELIER_OTEL_ENDPOINT`
- Google Cloud Logging exporter - optional collector-side export path in `deploy/otel-collector.yaml`
  - SDK/Client: collector `googlecloud` exporter
  - Auth: `GCP_PROJECT_ID`

**External Analyzer Sidecars:**

- Tokscale - optional CLI sidecar for usage/cost rollups, discovered and executed from `src/atelier/gateway/integrations/external_analytics.py`
  - SDK/Client: external CLI (`tokscale`)
  - Auth: Not managed by Atelier; binary path override `ATELIER_TOKSCALE_BIN`
- CodeBurn - optional CLI sidecar for efficiency analytics in `src/atelier/gateway/integrations/external_analytics.py`
  - SDK/Client: external CLI (`codeburn`)
  - Auth: Not managed by Atelier; binary path override `ATELIER_CODEBURN_BIN`
- ccusage - optional Claude-usage cross-check sidecar in `src/atelier/gateway/integrations/external_analytics.py`
  - SDK/Client: external CLI (`ccusage` / `npx ccusage`)
  - Auth: Not managed by Atelier; binary path override `ATELIER_CCUSAGE_BIN`

**Agent Host Integrations:**

- Claude Code, Codex CLI, Copilot, Gemini CLI, and opencode - packaged MCP/instruction integrations shipped under `integrations/` and installed by `scripts/install_*.sh`, with overview docs in `integrations/README.md` and `docs/hosts/all-agent-clis.md`
  - SDK/Client: `atelier-mcp`
  - Auth: local stdio mode uses no network auth; remote mode reuses `ATELIER_API_KEY`
- Session importers for additional host logs - ingestion/parsing support for `claude`, `codex`, `copilot`, `gemini`, `opencode`, `cursor`, `goose`, `qwen`, and others in `src/atelier/gateway/hosts/session_parsers/registry.py`
  - SDK/Client: internal parser classes under `src/atelier/gateway/hosts/session_parsers/`
  - Auth: Not applicable

## Data Storage

**Databases:**

- SQLite (default local store)
  - Connection: rooted at `ATELIER_ROOT` / `ATELIER_STORE_ROOT`, documented in `docs/installation.md`
  - Client: `ContextStore` / `SQLiteStore` in `src/atelier/core/foundation/store.py` and `src/atelier/infra/storage/sqlite_store.py`
- PostgreSQL (optional shared store)
  - Connection: `ATELIER_DATABASE_URL`
  - Client: `PostgresStore` with `psycopg` in `src/atelier/infra/storage/postgres_store.py`
- pgvector (optional Postgres extension)
  - Connection: same `ATELIER_DATABASE_URL`, gated by `ATELIER_VECTOR_SEARCH_ENABLED`
  - Client: `PostgresStore` vector DDL and search hooks in `src/atelier/infra/storage/postgres_store.py`

**File Storage:**

- Local filesystem is the primary artifact store under `ATELIER_ROOT`, including `atelier.db`, mirrored blocks/rubrics/traces, and runtime files described in `docs/installation.md`
- Local sidecar files include `vector_cache.sqlite` managed by `src/atelier/infra/storage/vector.py` and `openmemory_bridge.json` managed by `src/atelier/gateway/integrations/openmemory.py`
- Frontend static assets are built to `frontend/dist` and served by Nginx via `frontend/nginx.conf` and `Dockerfile.frontend`

**Caching:**

- No Redis or dedicated external cache is detected
- Local-only caches are used instead: embedding cache in `src/atelier/infra/storage/vector.py`, tool-result caching in `src/atelier/core/capabilities/tool_supervision/capability.py`, and browser `localStorage` for telemetry acknowledgement in `frontend/src/lib/insightsApi.ts`

## Authentication & Identity

**Auth Provider:**

- Custom Bearer auth
  - Implementation: FastAPI checks `Authorization: Bearer <ATELIER_API_KEY>` when `ATELIER_REQUIRE_AUTH=true` in `src/atelier/core/service/auth.py`, `src/atelier/core/service/config.py`, and `src/atelier/core/service/api.py`

## Monitoring & Observability

**Error Tracking:**

- None detected; no Sentry, Rollbar, or Bugsnag integration is present in `pyproject.toml`, `frontend/package.json`, or `src/`

**Logs:**

- Product telemetry is local-first and can export via OTel in `src/atelier/core/service/telemetry/`
- The frontend initializes PostHog/browser telemetry in `frontend/src/lib/telemetry.ts`
- Optional in-process Prometheus counters/histograms exist in `src/atelier/core/capabilities/tool_supervision/capability.py` and `src/atelier/core/capabilities/telemetry/context_budget.py`, but no scrape endpoint is exposed in `src/atelier/core/service/api.py`

## CI/CD & Deployment

**Hosting:**

- Self-hosted/local-first stack: FastAPI service + React frontend via Docker Compose in `docker-compose.yml`
- Installed product background management uses systemd on Linux and launchd on macOS in `README.md`, `docs/installation.md`, and `src/atelier/gateway/adapters/cli.py`
- Static frontend hosting is Nginx-based in `Dockerfile.frontend` and `frontend/nginx.conf`

**CI Pipeline:**

- GitHub Actions runs CodeQL, lint, typecheck, tests, and dependency audit in `.github/workflows/tests.yml`
- GitHub Actions runs docs governance checks in `.github/workflows/docs-governance.yml`

## Environment Configuration

**Required env vars:**

- Core service: `ATELIER_ROOT`, `ATELIER_SERVICE_HOST`, `ATELIER_SERVICE_PORT`, `ATELIER_REQUIRE_AUTH`, `ATELIER_API_KEY` in `src/atelier/core/service/config.py`
- Remote MCP/SDK: `ATELIER_MCP_MODE`, `ATELIER_SERVICE_URL` in `docs/sdk/mcp.md` and `src/atelier/gateway/adapters/remote_client.py`
- Storage/vector: `ATELIER_STORAGE_BACKEND`, `ATELIER_DATABASE_URL`, `ATELIER_VECTOR_SEARCH_ENABLED`, `ATELIER_EMBEDDING_PROVIDER`, `ATELIER_EMBEDDING_MODEL`, `ATELIER_EMBEDDING_DIM` in `docs/installation.md` and `src/atelier/infra/storage/vector.py`
- Model providers: `OPENAI_API_KEY`, `ATELIER_OPENAI_BASE_URL`, `ATELIER_OPENAI_API_KEY`, `ATELIER_OPENAI_MODEL`, `ATELIER_OLLAMA_MODEL` in `src/atelier/infra/internal_llm/openai_client.py` and `src/atelier/infra/internal_llm/ollama_client.py`
- Memory sidecars: `ATELIER_LETTA_URL`, `ATELIER_LETTA_API_KEY`, `ATELIER_OPENMEMORY_MCP_SERVER_NAME` in `src/atelier/infra/memory_bridges/letta_adapter.py` and `src/atelier/gateway/integrations/openmemory.py`
- Telemetry/UI: `ATELIER_OTEL_ENDPOINT`, `ATELIER_POSTHOG_KEY`, `ATELIER_POSTHOG_HOST`, `POSTHOG_OTLP_ENDPOINT`, `POSTHOG_PROJECT_API_KEY`, `GCP_PROJECT_ID`, `VITE_API_URL` in `src/atelier/core/service/telemetry/config.py`, `deploy/otel-collector.yaml`, `frontend/vite.config.ts`, and `scripts/worktree_env.py`
- External analyzer paths: `ATELIER_TOKSCALE_BIN`, `ATELIER_CODEBURN_BIN`, `ATELIER_CCUSAGE_BIN` in `src/atelier/gateway/integrations/external_analytics.py`

**Secrets location:**

- Secrets are environment-driven rather than checked into the repo, per `docs/installation.md`, `scripts/install.sh`, and the config readers in `src/atelier/core/service/config.py` and `src/atelier/core/service/telemetry/config.py`
- User-local telemetry state is written under the OS config directory by `src/atelier/core/foundation/identity.py` and `src/atelier/core/service/telemetry/config.py`

## Webhooks & Callbacks

**Incoming:**

- None detected; `src/atelier/core/service/api.py` exposes application APIs, but no third-party webhook receiver is defined

**Outgoing:**

- HTTPS calls to `https://api.openai.com/v1/embeddings` in `src/atelier/infra/embeddings/openai_embedder.py` and `src/atelier/infra/storage/vector.py`
- OpenAI-compatible chat calls to `ATELIER_OPENAI_BASE_URL` in `src/atelier/infra/internal_llm/openai_client.py`
- OTLP/PostHog export calls to `ATELIER_OTEL_ENDPOINT` or `POSTHOG_OTLP_ENDPOINT` in `src/atelier/core/service/telemetry/config.py` and `deploy/otel-collector.yaml`
- Letta health/API calls to `ATELIER_LETTA_URL` in `src/atelier/infra/memory_bridges/letta_adapter.py` and `src/atelier/gateway/adapters/cli.py`
- Remote Atelier service calls to `ATELIER_SERVICE_URL` in `src/atelier/gateway/adapters/remote_client.py` and `src/atelier/gateway/sdk/remote.py`
- External analyzer execution is subprocess-based rather than webhook-based in `src/atelier/gateway/integrations/external_analytics.py`

---

_Integration audit: 2026-05-18_
