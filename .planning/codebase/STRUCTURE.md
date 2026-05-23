# Codebase Structure

**Analysis Date:** 2026-05-23

## Directory Layout

```text
atelier/
├── src/                 # Python application/runtime source
├── tests/               # Python test suites mirroring src domains
├── frontend/            # Optional React/Vite dashboard
├── scripts/             # Install, verification, and maintenance scripts
├── docs/                # Active documentation
├── docs-archive/        # Historical/archived documentation
├── deploy/              # Deployment assets (service/otel/letta)
├── templates/           # Reusable template content
└── pyproject.toml       # Python package + entrypoints + tool config
```

## Directory Purposes

**src/atelier:**
- Purpose: Main runtime product package.
- Contains: Layered modules (`core`, `gateway`, `infra`, `sdk`).
- Key files: `src/atelier/core/runtime/engine.py`, `src/atelier/gateway/adapters/cli.py`, `src/atelier/core/service/api.py`.

**src/atelier/core:**
- Purpose: Domain logic and orchestration internals.
- Contains: capability modules, shared models, runtime engine, service API.
- Key files: `src/atelier/core/capabilities/__init__.py`, `src/atelier/core/foundation/store.py`, `src/atelier/core/service/worker.py`.

**src/atelier/gateway:**
- Purpose: External interface layer for CLI, MCP, hosts, SDK transport wrappers.
- Contains: adapters, host session parsers, SDK transport implementations.
- Key files: `src/atelier/gateway/adapters/mcp_server.py`, `src/atelier/gateway/adapters/runtime.py`, `src/atelier/gateway/adapters/remote_client.py`.

**src/atelier/infra:**
- Purpose: Concrete infrastructure implementations.
- Contains: storage backends, memory bridges, embeddings, runtime ledgers, code-intel adapters.
- Key files: `src/atelier/infra/storage/factory.py`, `src/atelier/infra/storage/sqlite_store.py`, `src/atelier/infra/runtime/run_ledger.py`.

**src/benchmarks:**
- Purpose: Benchmark executables and evaluation tooling.
- Contains: SWE, code-intel, and tool benchmark runners.
- Key files: `src/benchmarks/swe/run_swe_bench.py`, `src/benchmarks/tool_bench/runner.py`.

**tests/:**
- Purpose: Automated tests aligned with runtime layers and benchmark modules.
- Contains: `tests/core`, `tests/gateway`, `tests/infra`, `tests/benchmarks`.
- Key files: `tests/core/service/`, `tests/gateway/`, `tests/infra/code_intel/`.

**frontend/:**
- Purpose: Optional UI for runtime analytics and operations.
- Contains: React/Vite app (`frontend/src/pages`, `frontend/src/components`).
- Key files: `frontend/src/main.tsx`, `frontend/src/App.tsx`, `frontend/src/lib/insightsApi.ts`.

## Key File Locations

**Entry Points:**
- `pyproject.toml`: Defines console scripts `atelier` and `atelier-mcp`.
- `src/atelier/gateway/adapters/cli.py`: Primary CLI command tree and command implementations.
- `src/atelier/gateway/adapters/mcp_server.py`: MCP stdio server main loop and tool dispatch.
- `src/atelier/core/service/api.py`: FastAPI app factory and service startup entrypoint.

**Configuration:**
- `pyproject.toml`: Build, dependency, lint, and test config.
- `src/atelier/core/service/config.py`: Runtime service environment configuration.
- `src/atelier/core/environment.py`: Dev/stable tool gating and memory backend selection logic.

**Core Logic:**
- `src/atelier/core/runtime/engine.py`: Runtime orchestrator (`AtelierRuntimeCore`).
- `src/atelier/core/capabilities/`: Feature-specific capability implementations.
- `src/atelier/core/foundation/`: Shared domain models, renderers, retrievers, persistence base.

**Testing:**
- `tests/core/`: Core capability and service behavior tests.
- `tests/gateway/`: Adapter and host interaction tests.
- `tests/infra/`: Infrastructure backend and code-intel tests.
- `frontend/src/**/*.test.tsx`: Frontend UI and page tests.

## Naming Conventions

**Files:**
- Python modules use `snake_case.py` (`src/atelier/core/service/bootstrap_context.py`, `src/atelier/infra/storage/sqlite_memory_store.py`).
- Capability packages use noun directory + `capability.py` implementation (`src/atelier/core/capabilities/context_reuse/capability.py`).
- Frontend components/pages use `PascalCase.tsx` (`frontend/src/pages/Overview.tsx`, `frontend/src/components/WorkbenchUI.tsx`).

**Directories:**
- Runtime Python package directories are lowercase (`src/atelier/core`, `src/atelier/gateway`, `src/atelier/infra`).
- Test directories mirror runtime slices (`tests/core`, `tests/gateway`, `tests/infra`).

## Where to Add New Code

**New Feature:**
- Primary code: add capability logic under `src/atelier/core/capabilities/<feature>/` and wire in `src/atelier/core/runtime/engine.py`.
- Interface exposure:
  - CLI command in `src/atelier/gateway/adapters/cli.py`.
  - MCP tool in `src/atelier/gateway/adapters/mcp_server.py`.
  - HTTP endpoint in `src/atelier/core/service/api.py` (prefer delegating into runtime/capability layer).
- Tests: add mirrored tests in `tests/core/capabilities/<feature>/` plus interface-layer tests in `tests/gateway/` or `tests/core/service/`.

**New Component/Module:**
- Runtime/domain module: `src/atelier/core/<subdomain>/`.
- Infra implementation (storage/embeddings/code-intel): `src/atelier/infra/<area>/`.
- SDK surface update: `src/atelier/gateway/sdk/` and `src/atelier/sdk/__init__.py`.

**Utilities:**
- Shared domain helpers/models: `src/atelier/core/foundation/`.
- Transport helpers specific to interfaces: `src/atelier/gateway/adapters/`.
- Frontend shared helpers: `frontend/src/lib/`.

## Special Directories

**`src/atelier/infra/seed_blocks`:**
- Purpose: Bundled seed ReasonBlocks shipped with package builds.
- Generated: No.
- Committed: Yes.

**`src/atelier/core/rubrics`:**
- Purpose: Bundled rubric YAML assets used by runtime verification.
- Generated: No.
- Committed: Yes.

**`dist/`:**
- Purpose: Build artifacts and distributable outputs.
- Generated: Yes.
- Committed: Yes (present in repository).

**`.planning/codebase/`:**
- Purpose: Generated codebase maps used by planning/execution workflows.
- Generated: Yes.
- Committed: Yes.

---

*Structure analysis: 2026-05-23*
