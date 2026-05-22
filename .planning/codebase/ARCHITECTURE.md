<!-- refreshed: 2026-05-18 -->
# Architecture

**Analysis Date:** 2026-05-18

## System Overview

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Interface / Access Layer                          │
├──────────────────────┬──────────────────────┬───────────────────────────────┤
│     CLI adapter      │      MCP server      │      HTTP API + UI           │
│`src/atelier/gateway/ │`src/atelier/gateway/ │`src/atelier/core/service/`   │
│ adapters/cli.py`     │ adapters/mcp_server. │ `api.py` + `frontend/src/`   │
│                      │ py`                  │                               │
└───────────┬──────────┴──────────┬───────────┴──────────────┬────────────────┘
            │                     │                          │
            ▼                     ▼                          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Shared runtime / domain orchestration                    │
│ `src/atelier/gateway/adapters/runtime.py` + `src/atelier/core/runtime/`    │
│ `src/atelier/core/capabilities/` + `src/atelier/core/domains/`             │
└─────────────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                  Persistence, live state, and packaged data                 │
│ `src/atelier/core/foundation/store.py` + `src/atelier/infra/storage/`      │
│ `src/atelier/infra/runtime/` + workspace `.knowledge/`                     │
└─────────────────────────────────────────────────────────────────────────────┘
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

**Overall:** Ports-and-adapters around a shared runtime kernel.

**Key Characteristics:**
- Put agent-facing entry points in `src/atelier/gateway/`; keep reasoning and policy logic in `src/atelier/core/`.
- Route storage and runtime side effects through `src/atelier/core/foundation/store.py` and `src/atelier/infra/` instead of embedding persistence logic in entry points.
- Treat the frontend in `frontend/src/` as a separate consumer of the FastAPI surface in `src/atelier/core/service/api.py`.

## Layers

**Gateway layer:**
- Purpose: expose the product through CLI, MCP, SDK, and host/session adapters.
- Location: `src/atelier/gateway/`
- Contains: `adapters/`, `hosts/`, `integrations/`, `sdk/`
- Depends on: `src/atelier/core/`, `src/atelier/infra/`
- Used by: console scripts in `pyproject.toml`, host install assets in `integrations/`, external Python callers via `src/atelier/sdk/__init__.py`

**Core layer:**
- Purpose: own domain rules, runtime orchestration, capabilities, schemas, and service-level business logic.
- Location: `src/atelier/core/`
- Contains: `capabilities/`, `domains/`, `foundation/`, `runtime/`, `service/`
- Depends on: selected `infra` helpers plus standard libraries
- Used by: `src/atelier/gateway/`, `tests/core/`, `tests/gateway/`

**Infrastructure layer:**
- Purpose: implement persistence, memory backends, live ledgers, cost tracking, and other operational mechanics.
- Location: `src/atelier/infra/`
- Contains: `storage/`, `runtime/`, `embeddings/`, `memory_bridges/`, `tree_sitter/`
- Depends on: low-level libraries and environment configuration
- Used by: `src/atelier/core/runtime/engine.py`, `src/atelier/core/service/api.py`, `src/atelier/gateway/adapters/mcp_server.py`

**Frontend layer:**
- Purpose: render operational dashboards over the service API.
- Location: `frontend/src/`
- Contains: route pages in `frontend/src/pages/`, reusable UI in `frontend/src/components/`, API/client helpers in `frontend/src/api.ts` and `frontend/src/lib/`
- Depends on: FastAPI endpoints from `src/atelier/core/service/api.py`
- Used by: browser users and Docker/Vite entrypoints in `frontend/package.json`

## Data Flow

### Primary Request Path

1. Browser code issues `/api/...` requests through shared fetch helpers in `frontend/src/api.ts:13` and the Vite proxy in `frontend/vite.config.ts:16`.
2. FastAPI handlers are registered in `src/atelier/core/service/api.py:2681`, and each request lazily initializes the runtime store through `get_store()` in `src/atelier/core/service/api.py:2698`.
3. Persistent reads/writes land in the store root via `ContextStore.init()` and `ContextStore.record_trace()` in `src/atelier/core/foundation/store.py:328` and `src/atelier/core/foundation/store.py:780`.

### Host Tool Call Through MCP

1. `atelier-mcp` starts in `src/atelier/gateway/adapters/mcp_server.py:2722` and seeds workspace/service defaults before calling `serve()`.
2. MCP tools register through the `mcp_tool()` decorator and the module-level `TOOLS` registry in `src/atelier/gateway/adapters/mcp_server.py:72`.
3. Tool handlers reuse shared runtime/state objects from `ContextRuntime` in `src/atelier/gateway/adapters/runtime.py:340`, `RunLedger` in `src/atelier/infra/runtime/run_ledger.py:342`, and `RealtimeContextManager` in `src/atelier/infra/runtime/realtime_context.py:24`.

**State Management:**
- Durable runtime state lives under `ATELIER_ROOT` / `~/.atelier` by default via `src/atelier/core/foundation/paths.py:12`.
- Git-tracked knowledge lives separately under workspace `.knowledge/` via `src/atelier/core/foundation/paths.py:51`.
- Live session state is append-oriented: run ledgers persist to `runs/<session_id>.json` in `src/atelier/infra/runtime/run_ledger.py:381`, while the service can overlay live ledgers with imported traces in `src/atelier/core/service/api.py:4162`.

## Key Abstractions

**`ContextRuntime`:**
- Purpose: gateway-safe facade for starting sessions and calling runtime features without exposing capability wiring details.
- Examples: `src/atelier/gateway/adapters/runtime.py`, `src/atelier/gateway/sdk/local.py`
- Pattern: façade over `AtelierRuntimeCore`

**`AtelierRuntimeCore`:**
- Purpose: central composition root for context reuse, routing, loop detection, proof gating, semantic memory, and tool supervision.
- Examples: `src/atelier/core/runtime/engine.py`
- Pattern: orchestrator/service object

**`ContextStore` and storage factories:**
- Purpose: abstract backend selection while preserving a stable store contract for the rest of the codebase.
- Examples: `src/atelier/core/foundation/store.py`, `src/atelier/infra/storage/factory.py`, `src/atelier/infra/storage/sqlite_store.py`, `src/atelier/infra/storage/postgres_store.py`
- Pattern: repository + factory

**`AtelierClient`:**
- Purpose: normalized SDK contract across local, remote, and MCP-backed execution.
- Examples: `src/atelier/gateway/sdk/client.py`, `src/atelier/gateway/sdk/local.py`, `src/atelier/gateway/sdk/remote.py`
- Pattern: interface with transport-specific adapters

## Entry Points

**CLI (`atelier`):**
- Location: `pyproject.toml`, `src/atelier/gateway/adapters/cli.py`
- Triggers: shell command `atelier`
- Responsibilities: root command tree, service startup, worker control, imports, analytics, background-service installation

**MCP server (`atelier-mcp`):**
- Location: `pyproject.toml`, `src/atelier/gateway/adapters/mcp_server.py`
- Triggers: stdio-based MCP launch from supported hosts
- Responsibilities: tool registration, session ledger tracking, host detection, realtime context compaction

**HTTP service:**
- Location: `src/atelier/core/service/api.py`
- Triggers: `atelier service start`, Docker `service` container, direct uvicorn import-factory startup
- Responsibilities: expose runtime data and commands over HTTP, enforce optional bearer auth, aggregate live and persisted session state

**Frontend SPA:**
- Location: `frontend/src/main.tsx`, `frontend/src/App.tsx`
- Triggers: Vite dev server, frontend Docker/Nginx build
- Responsibilities: route rendering, shared time-window state, telemetry disclosure UI, dashboard navigation

**Python SDK:**
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

**What happens:** Most HTTP routes, compatibility shims, analytics aggregations, and session/report logic live in one file: `src/atelier/core/service/api.py`.
**Why it's wrong:** API-only changes and business-rule changes share the same module boundary, which raises merge pressure and makes route ownership hard to isolate.
**Do this instead:** Put new business logic in `src/atelier/core/` or `src/atelier/infra/`, then keep `src/atelier/core/service/api.py` as a thin request/response adapter.

### Duplicate CLI surface

**What happens:** `src/atelier/gateway/cli/__main__.py` defines a minimal fallback CLI, but installed entrypoints in `pyproject.toml:81` point to `src/atelier/gateway/adapters/cli.py`.
**Why it's wrong:** Adding commands to the fallback module does not change the shipped `atelier` command and creates two places that look authoritative.
**Do this instead:** Add real CLI behavior to `src/atelier/gateway/adapters/cli.py`; treat `src/atelier/gateway/cli/` as compatibility scaffolding only.

## Error Handling

**Strategy:** Fail fast at the interface layer, but keep storage migrations and optional integrations tolerant enough to preserve local operation.

**Patterns:**
- CLI surfaces convert failures into `click.ClickException` in `src/atelier/gateway/adapters/cli.py`.
- Service handlers raise `HTTPException` around auth and request validation in `src/atelier/core/service/api.py:2663`.
- Storage and runtime helpers selectively suppress migration/load failures when safe to continue, for example in `src/atelier/core/foundation/store.py:336` and `src/atelier/infra/runtime/realtime_context.py:190`.

## Cross-Cutting Concerns

**Logging:** Standard `logging.getLogger(__name__)` appears across layers, including `src/atelier/gateway/adapters/cli.py:63`, `src/atelier/core/service/api.py:42`, and `src/atelier/core/service/worker.py:22`.
**Validation:** Pydantic models define SDK contracts and request payloads in `src/atelier/gateway/sdk/client.py` and `src/atelier/core/service/schemas.py`.
**Authentication:** HTTP bearer auth is optional and centralized in `verify_api_key()` within `src/atelier/core/service/api.py:2663`; CLI and MCP remain local-process entry points.

---

*Architecture analysis: 2026-05-18*
