<!-- GSD:project-start source:PROJECT.md -->
## Project

**Atelier Code Intelligence**

Atelier Code Intelligence is the active brownfield program for extending Atelier's
existing CLI, MCP, service, and frontend surfaces with precomputed,
budget-aware code intelligence. It upgrades how agents find and change code so
symbol lookup, navigation, and targeted edits become near-zero-token operations
by default instead of repeated live-search work.

This project is for Atelier's agent-assisted coding workflows: first for
Atelier itself, then for repositories where Atelier-backed agents need fast,
deterministic code retrieval and editing primitives that scale better than
session-local LSP workflows.

**Core Value:** Agents can find and change code through budget-aware, precomputed intelligence
with near-zero token overhead by default.

### Constraints

- **Architecture**: Extend existing MCP tools and internal runtime modules
  before introducing new top-level tool registrations — `grounding.md` is the
  tie-breaker when milestone docs drift
- **Cost**: Every milestone must improve or protect token efficiency; outline
  first, cache aggressively, and make budgets explicit
- **Validation**: Milestones are not done without tests, benchmark evidence,
  validation-matrix coverage, and trace recording
- **Compatibility**: New code-intel behavior must fit Atelier's current
  Python/FastAPI/MCP/React architecture and preserve existing public entry
  points
- **Sequencing**: The full program scope is M0-M18, and the build-vs-integrate
  checkpoint in M18 must gate M16
<!-- GSD:project-end -->

<!-- GSD:stack-start source:codebase/STACK.md -->
## Technology Stack

## Languages
- Python 3.11+ - core runtime, CLI, MCP server, storage, telemetry, and benchmarks in `pyproject.toml`, `src/atelier/`, `src/benchmarks/`, and `scripts/`
- TypeScript 5.5 - React dashboard and browser-side API client in `frontend/package.json`, `frontend/tsconfig.json`, and `frontend/src/`
- Bash - install, verification, and host-integration automation in `Makefile` and `scripts/install.sh`, `scripts/install_*.sh`, `scripts/verify_*.sh`
- YAML/TOML/JSON config - packaging, Docker, workflows, and host config surfaces in `pyproject.toml`, `docker-compose.yml`, `deploy/otel-collector.yaml`, `.github/workflows/tests.yml`, and `integrations/`
## Runtime
- Python 3.11 minimum from `pyproject.toml`; CI runs Python 3.11 and 3.13 in `.github/workflows/tests.yml`
- API container uses Python 3.12 slim in `Dockerfile.api`
- Browser runtime for the dashboard, with Vite dev server on port 3125 in `frontend/vite.config.ts`
- `uv` - primary Python dependency manager and task runner in `uv.lock`, `Makefile`, `.github/workflows/tests.yml`, and `scripts/install.sh`
- Frontend package management is Bun-first in `Dockerfile.frontend` and `docker-compose.yml`, with npm-compatible metadata still committed in `frontend/package-lock.json`
- Lockfile: present (`uv.lock`, `frontend/bun.lock`, `frontend/package-lock.json`)
## Frameworks
- FastAPI 0.136.1+ - HTTP service app in `pyproject.toml` and `src/atelier/core/service/api.py`
- Click 8.1+ - CLI entrypoint for `atelier` in `pyproject.toml` and `src/atelier/gateway/adapters/cli.py`
- MCP stdio server - host-neutral tool transport in `src/atelier/gateway/adapters/mcp_server.py`
- React 18.3 + React Router 6 - dashboard UI bootstrapped in `frontend/package.json` and `frontend/src/main.tsx`
- pytest 8/9 - Python test runner configured in `pyproject.toml` and used by `Makefile`
- Vitest 2 - frontend unit test runner in `frontend/package.json` and `frontend/vite.config.ts`
- Testing Library - browser/component assertions in `frontend/package.json`
- Hatchling - Python build backend in `pyproject.toml`
- Vite 5 - frontend dev server and production bundler in `frontend/package.json` and `frontend/vite.config.ts`
- TypeScript 5.5 - strict frontend typechecking in `frontend/package.json` and `frontend/tsconfig.json`
- Tailwind CSS + PostCSS + Autoprefixer - dashboard styling pipeline in `frontend/package.json`, `frontend/tailwind.config.ts`, and `frontend/postcss.config.js`
- Docker Compose - local service/frontend stack orchestration in `docker-compose.yml`
- Nginx - static frontend serving in `Dockerfile.frontend` and `frontend/nginx.conf`
## Key Dependencies
- `pydantic` 2.6+ - data models and schema validation across runtime and SDK code in `pyproject.toml`, `src/atelier/core/foundation/`, and `src/atelier/gateway/sdk/`
- `fastapi` + `uvicorn[standard]` - service transport for the dashboard, remote SDK, and remote MCP mode in `pyproject.toml`, `src/atelier/core/service/api.py`, and `Dockerfile.api`
- `click` - command surface for install, service, memory, stack, and host-management workflows in `src/atelier/gateway/adapters/cli.py`
- `react`, `react-dom`, `react-router-dom` - dashboard shell and routing in `frontend/package.json` and `frontend/src/`
- `posthog-js` - frontend telemetry bootstrap in `frontend/package.json` and `frontend/src/lib/telemetry.ts`
- `psycopg[binary]` - optional PostgreSQL backend in `pyproject.toml` and `src/atelier/infra/storage/postgres_store.py`
- `pgvector` - optional vector column support for the Postgres backend in `pyproject.toml` and `src/atelier/infra/storage/postgres_store.py`
- `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` - backend telemetry export in `pyproject.toml` and `src/atelier/core/service/telemetry/`
- `prometheus-client` - optional in-process metrics counters/histograms in `pyproject.toml`, `src/atelier/core/capabilities/tool_supervision/capability.py`, and `src/atelier/core/capabilities/telemetry/context_budget.py`
- `tenacity` and `pybreaker` - retry/circuit-breaker controls for tool supervision in `pyproject.toml` and `src/atelier/core/capabilities/tool_supervision/capability.py`
- `openai`, `ollama`, `letta-client` - optional LLM, embedding, and memory sidecar integrations declared as extras in `pyproject.toml` and implemented under `src/atelier/infra/internal_llm/`, `src/atelier/infra/embeddings/`, and `src/atelier/infra/memory_bridges/`
## Configuration
- Service runtime config is environment-driven through `src/atelier/core/service/config.py` (`ATELIER_SERVICE_HOST`, `ATELIER_SERVICE_PORT`, `ATELIER_REQUIRE_AUTH`, `ATELIER_API_KEY`, `ATELIER_STORAGE_BACKEND`, `ATELIER_DATABASE_URL`, `ATELIER_ROOT`)
- Runtime policy and dev-mode gating come from `src/atelier/core/environment.py` (`ATELIER_DEV_MODE`, `ATELIER_PROFILE`)
- Telemetry opt-in/out state is stored under the user config directory by `src/atelier/core/service/telemetry/config.py`; repo docs for env knobs live in `docs/installation.md`
- Worktree-specific local stack values are generated by `scripts/worktree_env.py` (`ATELIER_SERVICE_PORT`, `ATELIER_FRONTEND_PORT`, `ATELIER_STACK_ROOT`, `VITE_API_URL`)
- Frontend API routing is configured in `frontend/vite.config.ts` and `frontend/nginx.conf`
- Python packaging and tool config live in `pyproject.toml`
- Frontend build config lives in `frontend/package.json`, `frontend/tsconfig.json`, `frontend/vite.config.ts`, `frontend/tailwind.config.ts`, and `frontend/postcss.config.js`
- Local container/runtime wiring lives in `Dockerfile.api`, `Dockerfile.frontend`, `docker-compose.yml`, and `deploy/otel-collector.yaml`
- CI automation is GitHub Actions in `.github/workflows/tests.yml` and `.github/workflows/docs-governance.yml`
## Platform Requirements
- Python 3.11+ with `uv` is required for backend tasks in `pyproject.toml`, `Makefile`, and `.github/workflows/tests.yml`
- Bun or npm is required only when working directly in `frontend/`, as shown in `frontend/README.md`
- Docker is optional but expected for the local service/frontend stack in `docker-compose.yml` and for Letta sidecar management in `deploy/letta/docker-compose.yml`
- systemd (Linux) or launchd (macOS) is expected when using the installed background-service mode described in `README.md`, `docs/installation.md`, and `src/atelier/gateway/adapters/cli.py`
- The installed product runs primarily as local CLI + MCP + OS-managed background services, without requiring HTTP by default, as documented in `README.md` and `docs/installation.md`
- The optional service deployment target is a FastAPI container on port 8787 plus a static React/Nginx frontend on port 3125 in `Dockerfile.api`, `Dockerfile.frontend`, and `docker-compose.yml`
- No cloud-specific IaC or managed deployment target is detected; the repository is optimized for local/self-hosted operation
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

## Naming Patterns
- Use `snake_case.py` for Python modules under `src/atelier/**` and `tests/**`, for example `src/atelier/core/environment.py` and `tests/gateway/test_service_api.py`.
- Use `PascalCase.tsx` for React components and pages under `frontend/src/**`, for example `frontend/src/App.tsx`, `frontend/src/pages/Reports.tsx`, and `frontend/src/components/WorkbenchUI.tsx`.
- Use `camelCase.ts` for frontend utility/API modules, for example `frontend/src/lib/insightsApi.ts` and `frontend/src/lib/utils.ts`.
- Name Python tests `test_*.py` in tiered folders such as `tests/core/`, `tests/gateway/`, `tests/infra/`, and `tests/docs/`. Name frontend tests `*.test.ts` or `*.test.tsx` beside the implementation, such as `frontend/src/pages/Sessions.test.tsx`.
- Use `snake_case` for Python functions and private helpers, with leading underscores for module-private helpers such as `_canonical_json` in `src/atelier/core/capabilities/code_context/engine.py` and `_bool_env` in `src/atelier/core/service/config.py`.
- Use `camelCase` for TypeScript functions such as `getTelemetryConfig`, `markLocalTelemetryAcknowledged`, and `renderReports` in `frontend/src/lib/insightsApi.ts` and `frontend/src/pages/Reports.test.tsx`.
- Use `PascalCase` for React components and Python classes, for example `CodeContextEngine` in `src/atelier/core/capabilities/code_context/engine.py`, `ServiceConfig` in `src/atelier/core/service/config.py`, and `Reports` in `frontend/src/pages/Reports.tsx`.
- Use `snake_case` for Python locals, parameters, and fixtures such as `seeded_runtime`, `tmp_path`, and `app_no_auth` in `tests/conftest.py` and `tests/gateway/test_service_api.py`.
- Use `camelCase` for frontend state and locals such as `contentErr`, `selected`, and `sampleSessions` in `frontend/src/pages/Reports.tsx` and `frontend/src/pages/Sessions.test.tsx`.
- Use `UPPER_SNAKE_CASE` for constants in both languages, for example `TRACE_FTS_COLUMNS` in `src/atelier/core/foundation/store.py`, `DEV_MODE_ENV_VAR` in `src/atelier/core/environment.py`, and `NAV_ITEMS` in `frontend/src/App.tsx`.
- Use `PascalCase` for Pydantic models, interfaces, and aliases such as `ReasonBlock` in `src/atelier/core/foundation/models.py`, `ContextRequest` in `src/atelier/core/service/schemas.py`, and `TelemetryConfig` in `frontend/src/lib/insightsApi.ts`.
- Use explicit literal/type aliases for constrained domains, for example `BlockStatus`, `TraceStatus`, and `Severity` in `src/atelier/core/foundation/models.py`.
## Code Style
- Python uses Ruff and Black from `pyproject.toml`.
- Ruff is configured at `pyproject.toml` with `line-length = 100`, `target-version = "py311"`, and lint families `E`, `F`, `I`, `B`, `UP`, `SIM`, and `RUF`.
- Black is configured at `pyproject.toml` with `line-length = 120`.
- Frontend formatting uses Prettier from `frontend/.prettierrc.json` with 2-space indentation, semicolons, double quotes, trailing commas `es5`, and `printWidth = 80`.
- `Makefile` routes formatting through `make format`, which runs `ruff --fix`, `black`, and `npx prettier --write` for `frontend/src/**/*.{ts,tsx,js,jsx,json,css,md}`.
- Python linting is enforced with Ruff via `make lint` in `Makefile`.
- Python type-checking is enforced with strict mypy via `make typecheck` and `[tool.mypy] strict = true` in `pyproject.toml`.
- Per-module mypy exceptions are explicit and localized, for example `src/atelier/gateway/adapters/cli.py` is listed under `ignore_errors = true` and API decorator-heavy modules are listed under `disable_error_code = ["untyped-decorator"]` in `pyproject.toml`.
- No standalone frontend ESLint or Biome configuration is detected. Frontend quality gates rely on `frontend/tsconfig.json` strict compiler options plus Vitest tests.
## Import Organization
- No TypeScript path aliases are detected in `frontend/tsconfig.json`.
- Frontend code uses relative imports such as `../lib/TimeRangeContext` in `frontend/src/pages/Sessions.test.tsx` and `./pages/Reports` in `frontend/src/App.tsx`.
- Python code imports by package path from `atelier.*`, for example `from atelier.core.foundation.models import ReasonBlock` in `src/atelier/gateway/adapters/cli.py`.
## Error Handling
- Raise specific Python exceptions for invalid inputs and unsupported runtime states, for example `raise ValueError("ATELIER_PROFILE must be 'stable' or 'dev'")` in `src/atelier/core/environment.py` and validator errors in `src/atelier/core/foundation/models.py`.
- Use `try/except` around optional dependencies and environment-sensitive behavior, returning safe fallbacks instead of crashing, for example `_git_repo_class()` in `src/atelier/core/capabilities/code_context/engine.py` and `_atelier_version()` in `src/atelier/gateway/adapters/cli.py`.
- Keep frontend API failure behavior centralized: `request<T>()` in `frontend/src/lib/insightsApi.ts` throws on non-OK responses, while pages decide whether to surface or suppress the error.
- Swallow non-critical UI failures only when loss is acceptable, for example telemetry acknowledgement and config reads in `frontend/src/App.tsx` use `.catch(() => undefined)`.
- Surface user-visible frontend failures through component state instead of logging, for example `setErr(String(e))` and `setContentErr(String(e))` in `frontend/src/pages/Reports.tsx`.
## Logging
- Define module-scoped loggers with `logging.getLogger(__name__)`, for example in `src/atelier/gateway/adapters/cli.py` and `src/atelier/core/foundation/store.py`.
- Prefer structured return values, stored traces, or HTTP responses over ad-hoc logs for normal control flow.
- Avoid `console.log` in frontend modules reviewed under `frontend/src/**`; UI state and rendered alerts are preferred.
## Comments
- Use module docstrings to declare responsibility and constraints, for example `src/atelier/core/environment.py`, `src/atelier/core/foundation/models.py`, and `src/atelier/gateway/adapters/cli.py`.
- Use section dividers in long Python modules and tests to separate concerns, such as `# --------------------------------------------------------------------------- #` blocks in `src/atelier/gateway/adapters/cli.py`, `src/atelier/core/foundation/store.py`, and `tests/gateway/test_service_api.py`.
- Keep inline comments sparse and purpose-driven. Frontend comments mostly explain UX or edge-case intent, such as the storage fallback note in `frontend/src/lib/insightsApi.ts` and section labels in `frontend/src/pages/Reports.tsx`.
- Python docstrings are common at module, class, and selected method level.
- JSDoc/TSDoc usage is minimal in `frontend/src/**`; TypeScript relies more on interfaces and explicit prop typing than comment blocks.
## Function Design
- Annotate Python parameters and returns consistently, including helpers and tests, as seen in `tests/gateway/test_cli.py`, `tests/core/service/test_api_week2_routes.py`, and `src/atelier/core/capabilities/code_context/engine.py`.
- Use keyword-only parameters in Python when optional behavior matters, for example `index_repo(..., include_globs=..., exclude_globs=..., force=True)` in `src/atelier/core/capabilities/code_context/engine.py`.
- Use typed props objects and generics in TypeScript, for example `request<T>()` in `frontend/src/lib/insightsApi.ts` and prop signatures in `frontend/src/components/WorkbenchUI.tsx`.
- Prefer Pydantic models for domain contracts in Python core code, for example `ReasonBlock` and `Trace` in `src/atelier/core/foundation/models.py`.
- Use plain dictionaries only for boundary layers or lightweight config/report payloads, for example `ServiceConfig.as_dict()` in `src/atelier/core/service/config.py`.
- Return typed `Promise<T>` values from frontend API helpers and keep the parsing step centralized in the helper module, as in `frontend/src/lib/insightsApi.ts`.
## Module Design
- Python modules export concrete classes/functions directly; consumers import from the owning module instead of broad barrel modules, for example `src/atelier/core/foundation/models.py` and `src/atelier/core/service/config.py`.
- Frontend pages default-export the page component, for example `frontend/src/pages/Reports.tsx` and `frontend/src/pages/Insights.tsx`.
- Shared frontend primitives use named exports, for example `frontend/src/components/WorkbenchUI.tsx` exports `Alert`, `Button`, `Card`, `Select`, and `ToggleGroup`.
- Frontend uses a selective barrel-style module in `frontend/src/components/WorkbenchUI.tsx` to re-export UI primitives and helper components.
- Python `__init__.py` files mostly mark packages rather than acting as large re-export layers, as seen across `src/atelier/**/__init__.py`.
- Preserve the package split between `src/atelier/core/**` for domain models/policies, `src/atelier/gateway/**` for adapters and host-facing surfaces, and `src/atelier/infra/**` for persistence/runtime integrations.
- Keep API schemas separate from core models: `src/atelier/core/service/schemas.py` wraps service boundary shapes while `src/atelier/core/foundation/models.py` holds runtime contracts.
- Keep tests aligned to the same tier split: `tests/core/**`, `tests/gateway/**`, `tests/infra/**`, and `tests/docs/**`.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

## System Overview
```text
```
## Component Responsibilities
| Component | Responsibility | File |
|-----------|----------------|------|
| CLI adapter | Installed `atelier` command surface, background-service management, import/export, worker/service startup | `src/atelier/gateway/adapters/cli.py` |
| MCP server | stdio JSON-RPC tool host for Claude/Codex/Gemini-style agents, plus live ledger/realtime context plumbing | `src/atelier/gateway/adapters/mcp_server.py` |
| HTTP service | FastAPI app exposing traces, memory, telemetry, analytics, sessions, rubrics, and compatibility endpoints | `src/atelier/core/service/api.py` |
| Runtime facade | Stable in-process API for agent sessions, context injection, rescue, routing, and trace recording | `src/atelier/gateway/adapters/runtime.py` |
| Runtime core | Composes capabilities such as context reuse, routing, tool supervision, proof gating, and semantic memory | `src/atelier/core/runtime/engine.py` |
| Persistent store | SQLite-first data layer with FTS, JSON mirrors, raw artifacts, jobs, rubrics, and trace persistence | `src/atelier/core/foundation/store.py` |
| Frontend SPA | React dashboard for overview, sessions, analytics, telemetry, memory, and reports | `frontend/src/App.tsx`, `frontend/src/api.ts` |
## Pattern Overview
- Put agent-facing entry points in `src/atelier/gateway/`; keep reasoning and policy logic in `src/atelier/core/`.
- Route storage and runtime side effects through `src/atelier/core/foundation/store.py` and `src/atelier/infra/` instead of embedding persistence logic in entry points.
- Treat the frontend in `frontend/src/` as a separate consumer of the FastAPI surface in `src/atelier/core/service/api.py`.
## Layers
- Purpose: expose the product through CLI, MCP, SDK, and host/session adapters.
- Location: `src/atelier/gateway/`
- Contains: `adapters/`, `hosts/`, `integrations/`, `sdk/`
- Depends on: `src/atelier/core/`, `src/atelier/infra/`
- Used by: console scripts in `pyproject.toml`, host install assets in `integrations/`, external Python callers via `src/atelier/sdk/__init__.py`
- Purpose: own domain rules, runtime orchestration, capabilities, schemas, and service-level business logic.
- Location: `src/atelier/core/`
- Contains: `capabilities/`, `domains/`, `foundation/`, `runtime/`, `service/`
- Depends on: selected `infra` helpers plus standard libraries
- Used by: `src/atelier/gateway/`, `tests/core/`, `tests/gateway/`
- Purpose: implement persistence, memory backends, live ledgers, cost tracking, and other operational mechanics.
- Location: `src/atelier/infra/`
- Contains: `storage/`, `runtime/`, `embeddings/`, `memory_bridges/`, `tree_sitter/`
- Depends on: low-level libraries and environment configuration
- Used by: `src/atelier/core/runtime/engine.py`, `src/atelier/core/service/api.py`, `src/atelier/gateway/adapters/mcp_server.py`
- Purpose: render operational dashboards over the service API.
- Location: `frontend/src/`
- Contains: route pages in `frontend/src/pages/`, reusable UI in `frontend/src/components/`, API/client helpers in `frontend/src/api.ts` and `frontend/src/lib/`
- Depends on: FastAPI endpoints from `src/atelier/core/service/api.py`
- Used by: browser users and Docker/Vite entrypoints in `frontend/package.json`
## Data Flow
### Primary Request Path
### Host Tool Call Through MCP
- Durable runtime state lives under `ATELIER_ROOT` / `~/.atelier` by default via `src/atelier/core/foundation/paths.py:12`.
- Git-tracked knowledge lives separately under workspace `.knowledge/` via `src/atelier/core/foundation/paths.py:51`.
- Live session state is append-oriented: run ledgers persist to `runs/<session_id>.json` in `src/atelier/infra/runtime/run_ledger.py:381`, while the service can overlay live ledgers with imported traces in `src/atelier/core/service/api.py:4162`.
## Key Abstractions
- Purpose: gateway-safe facade for starting sessions and calling runtime features without exposing capability wiring details.
- Examples: `src/atelier/gateway/adapters/runtime.py`, `src/atelier/gateway/sdk/local.py`
- Pattern: façade over `AtelierRuntimeCore`
- Purpose: central composition root for context reuse, routing, loop detection, proof gating, semantic memory, and tool supervision.
- Examples: `src/atelier/core/runtime/engine.py`
- Pattern: orchestrator/service object
- Purpose: abstract backend selection while preserving a stable store contract for the rest of the codebase.
- Examples: `src/atelier/core/foundation/store.py`, `src/atelier/infra/storage/factory.py`, `src/atelier/infra/storage/sqlite_store.py`, `src/atelier/infra/storage/postgres_store.py`
- Pattern: repository + factory
- Purpose: normalized SDK contract across local, remote, and MCP-backed execution.
- Examples: `src/atelier/gateway/sdk/client.py`, `src/atelier/gateway/sdk/local.py`, `src/atelier/gateway/sdk/remote.py`
- Pattern: interface with transport-specific adapters
## Entry Points
- Location: `pyproject.toml`, `src/atelier/gateway/adapters/cli.py`
- Triggers: shell command `atelier`
- Responsibilities: root command tree, service startup, worker control, imports, analytics, background-service installation
- Location: `pyproject.toml`, `src/atelier/gateway/adapters/mcp_server.py`
- Triggers: stdio-based MCP launch from supported hosts
- Responsibilities: tool registration, session ledger tracking, host detection, realtime context compaction
- Location: `src/atelier/core/service/api.py`
- Triggers: `atelier service start`, Docker `service` container, direct uvicorn import-factory startup
- Responsibilities: expose runtime data and commands over HTTP, enforce optional bearer auth, aggregate live and persisted session state
- Location: `frontend/src/main.tsx`, `frontend/src/App.tsx`
- Triggers: Vite dev server, frontend Docker/Nginx build
- Responsibilities: route rendering, shared time-window state, telemetry disclosure UI, dashboard navigation
- Location: `src/atelier/sdk/__init__.py`
- Triggers: external Python imports
- Responsibilities: stable public import path over `src/atelier/gateway/sdk/`
## Architectural Constraints
- **Threading:** mostly synchronous Python entry points; FastAPI uses a store-init lock in `src/atelier/core/service/api.py:2701`, `HostRegistry` uses `threading.RLock()` in `src/atelier/gateway/hosts/registry.py:31`, and the MCP server spawns an auto-update daemon thread in `src/atelier/gateway/adapters/mcp_server.py:2742`.
- **Global state:** module-level singletons exist in `src/atelier/core/service/config.py:82`, `src/atelier/core/service/api.py:5537`, and `src/atelier/gateway/adapters/mcp_server.py:150`; new code must assume process-global state in those modules.
- **Circular imports:** Not detected in explored files.
- **Storage boundary:** runtime data belongs under `ATELIER_ROOT` (`src/atelier/core/foundation/paths.py:12`), while repo-owned knowledge belongs under `.knowledge/` (`src/atelier/core/foundation/paths.py:51`).
- **Build order:** the Python package bundles seed blocks, rubrics, telemetry lexicon, and starter templates through `pyproject.toml:89`; the frontend builds separately from `frontend/package.json:7` and expects service-compatible `/api` endpoints through `frontend/vite.config.ts:16`.
## Anti-Patterns
### Monolithic service module
### Duplicate CLI surface
## Error Handling
- CLI surfaces convert failures into `click.ClickException` in `src/atelier/gateway/adapters/cli.py`.
- Service handlers raise `HTTPException` around auth and request validation in `src/atelier/core/service/api.py:2663`.
- Storage and runtime helpers selectively suppress migration/load failures when safe to continue, for example in `src/atelier/core/foundation/store.py:336` and `src/atelier/infra/runtime/realtime_context.py:190`.
## Cross-Cutting Concerns
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->
## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
