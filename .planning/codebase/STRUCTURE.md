# Codebase Structure

**Analysis Date:** 2026-05-18

## Directory Layout

```text
[project-root]/
├── `src/atelier/`              # Main Python product package
├── `src/benchmarks/`           # Packaged benchmark modules shipped with the wheel
├── `frontend/src/`             # React/Vite dashboard application
├── `tests/`                    # Python pytest suites for core, gateway, infra, docs
├── `scripts/`                  # Installers, sync tools, verification scripts, hooks
├── `integrations/`             # Host-specific install surfaces and skill sources
├── `templates/reasonblocks/`   # Starter template bundles copied by CLI init flows
├── `.knowledge/`               # Repo-local knowledge/rubric content
├── `docs/`                     # Active product and engineering documentation
└── `benchmarks/`               # Top-level harness assets and wrappers
```

## Directory Purposes

**`src/atelier/core/`:**
- Purpose: reasoning/runtime business logic.
- Contains: capabilities, domain bundles, foundational models/store, runtime engine, service API.
- Key files: `src/atelier/core/runtime/engine.py`, `src/atelier/core/foundation/store.py`, `src/atelier/core/service/api.py`, `src/atelier/core/domains/manager.py`

**`src/atelier/gateway/`:**
- Purpose: entry points and external access adapters.
- Contains: CLI/MCP adapters, host/session parsers, SDK clients, host-facing integrations.
- Key files: `src/atelier/gateway/adapters/cli.py`, `src/atelier/gateway/adapters/mcp_server.py`, `src/atelier/gateway/adapters/runtime.py`, `src/atelier/gateway/hosts/session_parsers/registry.py`

**`src/atelier/infra/`:**
- Purpose: operational implementation details beneath the core contracts.
- Contains: storage backends, runtime ledgers, embeddings, memory bridges, seed data, tree-sitter helpers.
- Key files: `src/atelier/infra/storage/factory.py`, `src/atelier/infra/storage/sqlite_store.py`, `src/atelier/infra/storage/postgres_store.py`, `src/atelier/infra/runtime/run_ledger.py`, `src/atelier/infra/runtime/realtime_context.py`

**`src/atelier/sdk/`:**
- Purpose: public import surface for Python consumers.
- Contains: re-exports over `src/atelier/gateway/sdk/`
- Key files: `src/atelier/sdk/__init__.py`

**`src/benchmarks/`:**
- Purpose: packaged benchmark code that ships with the Python wheel.
- Contains: SWE benchmark runners, datasets, configs, retrieval ground truth.
- Key files: `src/benchmarks/swe/run_swe_bench.py`, `src/benchmarks/swe/config.py`, `src/benchmarks/retrieval/ground_truth.jsonl`

**`frontend/src/`:**
- Purpose: browser UI for operational visibility.
- Contains: route pages, components, API clients, shared state/context, tests.
- Key files: `frontend/src/main.tsx`, `frontend/src/App.tsx`, `frontend/src/api.ts`, `frontend/src/lib/insightsApi.ts`

**`tests/`:**
- Purpose: backend regression coverage split by architectural layer.
- Contains: `tests/core/`, `tests/gateway/`, `tests/infra/`, `tests/docs/`, fixtures.
- Key files: `tests/gateway/test_service_api.py`, `tests/gateway/test_mcp_tool_handlers.py`, `tests/core/test_capabilities_runtime_core.py`, `tests/infra/test_store.py`

**`scripts/`:**
- Purpose: developer lifecycle, installer, verification, and sync automation.
- Contains: shell installers, verification scripts, Python maintenance utilities, shell hooks.
- Key files: `scripts/install.sh`, `scripts/sync_agent_context.py`, `scripts/worktree_env.py`, `scripts/verify_atelier_service.sh`, `scripts/hooks/tool_redirect_hook.py`

**`integrations/`:**
- Purpose: generated or source-controlled install surfaces for supported agent hosts.
- Contains: host directories plus skill sources under `integrations/skills/`
- Key files: `integrations/README.md`, `integrations/skills/`, `integrations/claude/`, `integrations/codex/`

**`.knowledge/`:**
- Purpose: project-local, Git-tracked knowledge content separated from runtime state.
- Contains: `blocks/`, `rubrics/`
- Key files: `.knowledge/blocks/`, `.knowledge/rubrics/`

## Key File Locations

**Entry Points:**
- `src/atelier/gateway/adapters/cli.py`: installed `atelier` command tree
- `src/atelier/gateway/adapters/mcp_server.py`: installed `atelier-mcp` stdio server
- `src/atelier/core/service/api.py`: FastAPI factory and uvicorn launcher
- `frontend/src/main.tsx`: SPA bootstrap
- `src/atelier/sdk/__init__.py`: public Python SDK namespace

**Configuration:**
- `pyproject.toml`: Python package metadata, scripts, tool config, package data inclusion
- `frontend/package.json`: frontend scripts and dependency graph
- `frontend/vite.config.ts`: dev server and `/api` proxy wiring
- `docker-compose.yml`: local service + frontend stack orchestration
- `Dockerfile.api`: backend image build
- `Dockerfile.frontend`: frontend static build image
- `Makefile`: common dev/test/install targets

**Core Logic:**
- `src/atelier/core/runtime/engine.py`: composition root for runtime capabilities
- `src/atelier/gateway/adapters/runtime.py`: gateway-safe façade over the runtime
- `src/atelier/core/foundation/store.py`: canonical SQLite/FTS data model and persistence behavior
- `src/atelier/core/service/worker.py`: job processor
- `src/atelier/core/capabilities/__init__.py`: capability registry/export map

**Testing:**
- `tests/core/`: runtime, models, retrieval, routing, capability coverage
- `tests/gateway/`: CLI, MCP, API, installation/integration coverage
- `tests/infra/`: storage, runtime ledger, memory backend, benchmark infrastructure
- `frontend/src/pages/*.test.tsx`: route-level React tests
- `frontend/src/lib/*.test.ts`: focused frontend helper tests

## Naming Conventions

**Files:**
- Python modules use `snake_case.py`: `src/atelier/core/foundation/store.py`, `src/atelier/gateway/hosts/session_parsers/registry.py`
- React pages/components use `PascalCase.tsx`: `frontend/src/pages/Sessions.tsx`, `frontend/src/components/MemoryBlockCard.tsx`
- Frontend helper modules use lowercase or mixed camel names for utilities/APIs: `frontend/src/api.ts`, `frontend/src/lib/insightsApi.ts`, `frontend/src/lib/runtimeCatalog.ts`

**Directories:**
- Backend directories are capability- or boundary-oriented: `src/atelier/core/capabilities/`, `src/atelier/gateway/hosts/`, `src/atelier/infra/storage/`
- Frontend directories are role-oriented: `frontend/src/pages/`, `frontend/src/components/`, `frontend/src/lib/`
- Tests mirror product boundaries instead of source-tree paths exactly: `tests/core/`, `tests/gateway/`, `tests/infra/`

## Where to Add New Code

**New Feature:**
- Primary backend logic: `src/atelier/core/` if it changes reasoning/routing/policy, or `src/atelier/infra/` if it changes persistence/runtime mechanics
- API surface: `src/atelier/core/service/api.py` with request/response models in `src/atelier/core/service/schemas.py`
- Gateway exposure: `src/atelier/gateway/adapters/cli.py` for CLI, `src/atelier/gateway/adapters/mcp_server.py` for MCP, `src/atelier/gateway/sdk/` for SDK transport changes
- Tests: `tests/core/`, `tests/gateway/`, or `tests/infra/` matching the layer you touched

**New Component/Module:**
- Runtime capability: add under `src/atelier/core/capabilities/` and wire it through `src/atelier/core/capabilities/__init__.py` plus `src/atelier/core/runtime/engine.py`
- Host/session importer: add parser under `src/atelier/gateway/hosts/session_parsers/` and register it in `src/atelier/gateway/hosts/session_parsers/registry.py`
- Storage backend or persistence extension: add under `src/atelier/infra/storage/` and select it through `src/atelier/infra/storage/factory.py`
- Frontend page: add `frontend/src/pages/<Name>.tsx` and register its route in `frontend/src/App.tsx`

**Utilities:**
- Shared backend helpers: `src/atelier/core/foundation/` for domain-agnostic logic or `src/atelier/infra/runtime/` for operational helpers
- Frontend shared helpers: `frontend/src/lib/`
- Developer automation: `scripts/` for repo tooling, not `src/atelier/`

## Special Directories

**`.knowledge/`:**
- Purpose: workspace-owned knowledge base resolved by `src/atelier/core/foundation/paths.py`
- Generated: No
- Committed: Yes

**`templates/reasonblocks/`:**
- Purpose: starter templates copied by CLI init flows such as `atelier init --stack ...`
- Generated: No
- Committed: Yes

**`integrations/skills/`:**
- Purpose: host-skill source material consumed by `scripts/build_host_skills.sh`
- Generated: No
- Committed: Yes

**`src/atelier/infra/seed_blocks/`:**
- Purpose: bundled seed knowledge included in package builds through `pyproject.toml`
- Generated: No
- Committed: Yes

**`src/benchmarks/swe/outputs/`:**
- Purpose: destination for benchmark run artifacts while preserving the folder in the packaged benchmark tree
- Generated: Yes
- Committed: Yes

---

*Structure analysis: 2026-05-18*
