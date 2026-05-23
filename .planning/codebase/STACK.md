# Technology Stack

**Analysis Date:** 2026-05-23

## Languages

**Primary:**
- Python 3.11+ - Core runtime, CLI, MCP server, API, storage, and telemetry in `src/atelier/` (`pyproject.toml`, `src/atelier/core/service/api.py`, `src/atelier/gateway/adapters/cli.py`)

**Secondary:**
- TypeScript - Frontend dashboard and client integrations in `frontend/src/` (`frontend/package.json`, `frontend/src/main.tsx`, `frontend/src/api.ts`)
- JavaScript (ESM) - Frontend tooling scripts/config in `frontend/` (`frontend/scripts/run-vitest.mjs`, `frontend/vite.config.ts`)
- YAML/TOML - CI, deployment, and project config (`.github/workflows/tests.yml`, `deploy/otel-collector.yaml`, `pyproject.toml`)

## Runtime

**Environment:**
- Python runtime: `>=3.11` (`pyproject.toml`, `uv.lock`)
- Node.js runtime for frontend tooling (`frontend/package.json`, `frontend/README.md`)
- Bun runtime used in frontend Docker build/dev flows (`Dockerfile.frontend`, `docker-compose.yml`)

**Package Manager:**
- Python: `uv` lock-based workflow (`uv.lock`, `pyproject.toml`, `.github/workflows/tests.yml`)
- Frontend: npm-compatible lockfile present (`frontend/package-lock.json`) with npm/bun scripts (`frontend/package.json`, `Dockerfile.frontend`)
- Lockfile: present (`uv.lock`, `frontend/package-lock.json`)

## Frameworks

**Core:**
- FastAPI `>=0.136.1` - HTTP service layer (`pyproject.toml`, `src/atelier/core/service/api.py`)
- Uvicorn `>=0.46.0` - ASGI server runner (`pyproject.toml`, `src/atelier/core/service/api.py`)
- React `^18.3.1` + React Router `^6.26.0` - Dashboard UI (`frontend/package.json`, `frontend/src/main.tsx`)
- Vite `^5.3.4` - Frontend dev/build tooling (`frontend/package.json`, `frontend/vite.config.ts`)

**Testing:**
- Pytest `>=8` - Backend tests (`pyproject.toml`, `[tool.pytest.ini_options]`, `Makefile`)
- Vitest `^2.1.5` + Testing Library - Frontend tests (`frontend/package.json`, `frontend/scripts/run-vitest.mjs`)

**Build/Dev:**
- Ruff `>=0.5` - Linting (`pyproject.toml`, `Makefile`)
- Black `>=24.4` - Formatting (`pyproject.toml`, `Makefile`)
- MyPy `strict` - Type checking (`pyproject.toml`, `Makefile`, `.github/workflows/tests.yml`)
- Docker / Docker Compose - Local stack and containerized runtime (`Dockerfile.api`, `Dockerfile.frontend`, `docker-compose.yml`)

## Key Dependencies

**Critical:**
- `fastapi`, `uvicorn` - Service API and runtime (`pyproject.toml`, `src/atelier/core/service/api.py`)
- `pydantic`, `pydantic-settings` - Typed models/config (`pyproject.toml`, `src/atelier/core/service/config.py`)
- `pyyaml` - YAML-driven host/integration config (`pyproject.toml`, `src/atelier/gateway/hosts/configs/*.yaml`)
- `posthog-js` - Frontend telemetry client (`frontend/package.json`, `frontend/src/lib/telemetry.ts`)

**Infrastructure:**
- `opentelemetry-*` - Remote telemetry export pipeline (`pyproject.toml`, `src/atelier/core/service/telemetry/exporters/otel.py`)
- `prometheus-client` - Metrics dependency declared for runtime instrumentation (`pyproject.toml`)
- `psycopg[binary]` (optional) - Postgres backend (`pyproject.toml`, `src/atelier/infra/storage/postgres_store.py`)
- `letta-client` / `letta` (optional extras) - Letta sidecar memory integrations (`pyproject.toml`, `src/atelier/infra/memory_bridges/letta_adapter.py`)
- `ollama`, `openai` (optional/cloud extras) - Internal LLM and embedding providers (`pyproject.toml`, `src/atelier/infra/internal_llm/openai_client.py`, `src/atelier/infra/internal_llm/ollama_client.py`)

## Configuration

**Environment:**
- Service config from environment variables via `ServiceConfig` (`src/atelier/core/service/config.py`)
- Telemetry and PostHog config via env + telemetry TOML (`src/atelier/core/service/telemetry/config.py`)
- Frontend API target via `VITE_API_URL` and `/api` proxy (`frontend/vite.config.ts`, `frontend/src/api.ts`)
- `.env.production.example` file present for environment setup (existence noted only)

**Build:**
- Python project/build metadata in `pyproject.toml`
- Frontend build/test config in `frontend/package.json`, `frontend/vite.config.ts`, `frontend/tsconfig.json`
- Container build/runtime config in `Dockerfile.api`, `Dockerfile.frontend`, `docker-compose.yml`
- CI build/verify pipeline in `.github/workflows/tests.yml`

## Platform Requirements

**Development:**
- Linux/macOS/Windows host support for CLI integrations (`src/atelier/gateway/hosts/configs/*.yaml`)
- Python 3.11+ with `uv` for backend (`pyproject.toml`, `uv.lock`)
- Node/npm (or Bun) for frontend (`frontend/README.md`, `frontend/package.json`)
- Optional Docker for compose-based stack (`docker-compose.yml`)

**Production:**
- ASGI service process (`uvicorn`) exposing port `8787` (`src/atelier/core/service/api.py`, `Dockerfile.api`)
- Optional static frontend served by nginx on `3125` (`Dockerfile.frontend`)
- Optional containerized deployment with compose (`docker-compose.yml`)

---

*Stack analysis: 2026-05-23*
