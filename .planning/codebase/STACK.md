# Technology Stack

**Analysis Date:** 2026-05-18

## Languages

**Primary:**
- Python 3.11+ - core runtime, CLI, MCP server, storage, telemetry, and benchmarks in `pyproject.toml`, `src/atelier/`, `src/benchmarks/`, and `scripts/`

**Secondary:**
- TypeScript 5.5 - React dashboard and browser-side API client in `frontend/package.json`, `frontend/tsconfig.json`, and `frontend/src/`
- Bash - install, verification, and host-integration automation in `Makefile` and `scripts/install.sh`, `scripts/install_*.sh`, `scripts/verify_*.sh`
- YAML/TOML/JSON config - packaging, Docker, workflows, and host config surfaces in `pyproject.toml`, `docker-compose.yml`, `deploy/otel-collector.yaml`, `.github/workflows/tests.yml`, and `integrations/`

## Runtime

**Environment:**
- Python 3.11 minimum from `pyproject.toml`; CI runs Python 3.11 and 3.13 in `.github/workflows/tests.yml`
- API container uses Python 3.12 slim in `Dockerfile.api`
- Browser runtime for the dashboard, with Vite dev server on port 3125 in `frontend/vite.config.ts`

**Package Manager:**
- `uv` - primary Python dependency manager and task runner in `uv.lock`, `Makefile`, `.github/workflows/tests.yml`, and `scripts/install.sh`
- Frontend package management is Bun-first in `Dockerfile.frontend` and `docker-compose.yml`, with npm-compatible metadata still committed in `frontend/package-lock.json`
- Lockfile: present (`uv.lock`, `frontend/bun.lock`, `frontend/package-lock.json`)

## Frameworks

**Core:**
- FastAPI 0.136.1+ - HTTP service app in `pyproject.toml` and `src/atelier/core/service/api.py`
- Click 8.1+ - CLI entrypoint for `atelier` in `pyproject.toml` and `src/atelier/gateway/adapters/cli.py`
- MCP stdio server - host-neutral tool transport in `src/atelier/gateway/adapters/mcp_server.py`
- React 18.3 + React Router 6 - dashboard UI bootstrapped in `frontend/package.json` and `frontend/src/main.tsx`

**Testing:**
- pytest 8/9 - Python test runner configured in `pyproject.toml` and used by `Makefile`
- Vitest 2 - frontend unit test runner in `frontend/package.json` and `frontend/vite.config.ts`
- Testing Library - browser/component assertions in `frontend/package.json`

**Build/Dev:**
- Hatchling - Python build backend in `pyproject.toml`
- Vite 5 - frontend dev server and production bundler in `frontend/package.json` and `frontend/vite.config.ts`
- TypeScript 5.5 - strict frontend typechecking in `frontend/package.json` and `frontend/tsconfig.json`
- Tailwind CSS + PostCSS + Autoprefixer - dashboard styling pipeline in `frontend/package.json`, `frontend/tailwind.config.ts`, and `frontend/postcss.config.js`
- Docker Compose - local service/frontend stack orchestration in `docker-compose.yml`
- Nginx - static frontend serving in `Dockerfile.frontend` and `frontend/nginx.conf`

## Key Dependencies

**Critical:**
- `pydantic` 2.6+ - data models and schema validation across runtime and SDK code in `pyproject.toml`, `src/atelier/core/foundation/`, and `src/atelier/gateway/sdk/`
- `fastapi` + `uvicorn[standard]` - service transport for the dashboard, remote SDK, and remote MCP mode in `pyproject.toml`, `src/atelier/core/service/api.py`, and `Dockerfile.api`
- `click` - command surface for install, service, memory, stack, and host-management workflows in `src/atelier/gateway/adapters/cli.py`
- `react`, `react-dom`, `react-router-dom` - dashboard shell and routing in `frontend/package.json` and `frontend/src/`
- `posthog-js` - frontend telemetry bootstrap in `frontend/package.json` and `frontend/src/lib/telemetry.ts`

**Infrastructure:**
- `psycopg[binary]` - optional PostgreSQL backend in `pyproject.toml` and `src/atelier/infra/storage/postgres_store.py`
- `pgvector` - optional vector column support for the Postgres backend in `pyproject.toml` and `src/atelier/infra/storage/postgres_store.py`
- `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` - backend telemetry export in `pyproject.toml` and `src/atelier/core/service/telemetry/`
- `prometheus-client` - optional in-process metrics counters/histograms in `pyproject.toml`, `src/atelier/core/capabilities/tool_supervision/capability.py`, and `src/atelier/core/capabilities/telemetry/context_budget.py`
- `tenacity` and `pybreaker` - retry/circuit-breaker controls for tool supervision in `pyproject.toml` and `src/atelier/core/capabilities/tool_supervision/capability.py`
- `openai`, `ollama`, `letta-client` - optional LLM, embedding, and memory sidecar integrations declared as extras in `pyproject.toml` and implemented under `src/atelier/infra/internal_llm/`, `src/atelier/infra/embeddings/`, and `src/atelier/infra/memory_bridges/`

## Configuration

**Environment:**
- Service runtime config is environment-driven through `src/atelier/core/service/config.py` (`ATELIER_SERVICE_HOST`, `ATELIER_SERVICE_PORT`, `ATELIER_REQUIRE_AUTH`, `ATELIER_API_KEY`, `ATELIER_STORAGE_BACKEND`, `ATELIER_DATABASE_URL`, `ATELIER_ROOT`)
- Runtime policy and dev-mode gating come from `src/atelier/core/environment.py` (`ATELIER_DEV_MODE`, `ATELIER_PROFILE`)
- Telemetry opt-in/out state is stored under the user config directory by `src/atelier/core/service/telemetry/config.py`; repo docs for env knobs live in `docs/installation.md`
- Worktree-specific local stack values are generated by `scripts/worktree_env.py` (`ATELIER_SERVICE_PORT`, `ATELIER_FRONTEND_PORT`, `ATELIER_STACK_ROOT`, `VITE_API_URL`)
- Frontend API routing is configured in `frontend/vite.config.ts` and `frontend/nginx.conf`

**Build:**
- Python packaging and tool config live in `pyproject.toml`
- Frontend build config lives in `frontend/package.json`, `frontend/tsconfig.json`, `frontend/vite.config.ts`, `frontend/tailwind.config.ts`, and `frontend/postcss.config.js`
- Local container/runtime wiring lives in `Dockerfile.api`, `Dockerfile.frontend`, `docker-compose.yml`, and `deploy/otel-collector.yaml`
- CI automation is GitHub Actions in `.github/workflows/tests.yml` and `.github/workflows/docs-governance.yml`

## Platform Requirements

**Development:**
- Python 3.11+ with `uv` is required for backend tasks in `pyproject.toml`, `Makefile`, and `.github/workflows/tests.yml`
- Bun or npm is required only when working directly in `frontend/`, as shown in `frontend/README.md`
- Docker is optional but expected for the local service/frontend stack in `docker-compose.yml` and for Letta sidecar management in `deploy/letta/docker-compose.yml`
- systemd (Linux) or launchd (macOS) is expected when using the installed background-service mode described in `README.md`, `docs/installation.md`, and `src/atelier/gateway/adapters/cli.py`

**Production:**
- The installed product runs primarily as local CLI + MCP + OS-managed background services, without requiring HTTP by default, as documented in `README.md` and `docs/installation.md`
- The optional service deployment target is a FastAPI container on port 8787 plus a static React/Nginx frontend on port 3125 in `Dockerfile.api`, `Dockerfile.frontend`, and `docker-compose.yml`
- No cloud-specific IaC or managed deployment target is detected; the repository is optimized for local/self-hosted operation

---

*Stack analysis: 2026-05-18*
