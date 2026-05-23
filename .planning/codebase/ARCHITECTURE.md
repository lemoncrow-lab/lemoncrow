<!-- refreshed: 2026-05-23 -->
# Architecture

**Analysis Date:** 2026-05-23

## System Overview

```text
┌─────────────────────────────────────────────────────────────┐
│                 Gateway / Interface Layer                  │
├──────────────────┬──────────────────┬───────────────────────┤
│   CLI Adapter    │   MCP Adapter    │      Service API       │
│`src/atelier/...` │`src/atelier/...` │ `src/atelier/...`      │
│`gateway/adapters/│`gateway/adapters/│ `core/service/api.py`  │
│cli.py`           │mcp_server.py`    │                        │
└────────┬─────────┴────────┬─────────┴──────────┬────────────┘
         │                  │                     │
         ▼                  ▼                     ▼
┌─────────────────────────────────────────────────────────────┐
│              Core Runtime + Capabilities Layer              │
│   `src/atelier/core/runtime/engine.py`                      │
│   `src/atelier/core/capabilities/*`                         │
└─────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│                Storage / Infra / Runtime State              │
│`src/atelier/core/foundation/store.py`                       │
│`src/atelier/infra/storage/*` `src/atelier/infra/runtime/*` │
└─────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| Gateway adapters | Accept CLI/MCP/HTTP requests and map them to runtime operations | `src/atelier/gateway/adapters/cli.py`, `src/atelier/gateway/adapters/mcp_server.py`, `src/atelier/core/service/api.py` |
| Runtime orchestrator | Compose capabilities into one façade (`get_context`, `rescue_failure`, `smart_*`, routing) | `src/atelier/core/runtime/engine.py` |
| Capability modules | Implement isolated domain logic (context reuse, routing, proof gate, recall, supervision) | `src/atelier/core/capabilities/context_reuse/capability.py`, `src/atelier/core/capabilities/quality_router/capability.py`, `src/atelier/core/capabilities/proof_gate/capability.py` |
| Persistent store | Persist traces/blocks/rubrics/jobs in SQLite + FTS, mirror artifacts to filesystem | `src/atelier/core/foundation/store.py` |
| Backend factory | Select storage/memory backend via env/config and return concrete implementation | `src/atelier/infra/storage/factory.py` |

## Pattern Overview

**Overall:** Layered modular monolith with adapter + façade orchestration.

**Key Characteristics:**
- Interface adapters defer runtime logic into `ContextRuntime` / `AtelierRuntimeCore` (`src/atelier/gateway/adapters/runtime.py`).
- Capabilities are packaged as focused modules under `src/atelier/core/capabilities/` and assembled centrally in `AtelierRuntimeCore.__init__` (`src/atelier/core/runtime/engine.py:53`).
- Persistence is centralized through `ContextStore` and memory-store protocols (`src/atelier/core/foundation/store.py`, `src/atelier/infra/storage/memory_store.py`).

## Layers

**Gateway Layer:**
- Purpose: Handle transport concerns (Click CLI, MCP JSON-RPC, FastAPI HTTP).
- Location: `src/atelier/gateway/` and `src/atelier/core/service/`.
- Contains: Command wiring, request validation, protocol shaping, telemetry/session hooks.
- Depends on: `src/atelier/gateway/adapters/runtime.py`, core models, service config/auth.
- Used by: CLI entrypoint (`pyproject.toml` `[project.scripts]`), MCP host processes, HTTP clients.

**Core Runtime Layer:**
- Purpose: Orchestrate capability calls into deterministic runtime behavior.
- Location: `src/atelier/core/runtime/engine.py`.
- Contains: `AtelierRuntimeCore`, context assembly, routing, smart tools, SQL inspection.
- Depends on: capability packages (`src/atelier/core/capabilities/*`) and store abstractions.
- Used by: `ContextRuntime` (`src/atelier/gateway/adapters/runtime.py`) and SDK clients.

**Domain Foundations Layer:**
- Purpose: Define shared domain models and reusable primitives.
- Location: `src/atelier/core/foundation/`.
- Contains: Pydantic models, renderers, retrievers, rubric gate, path resolution, storage class.
- Depends on: stdlib + pydantic/yaml/sqlite modules.
- Used by: all higher layers.

**Infrastructure Layer:**
- Purpose: Provide concrete integrations (storage implementations, embeddings, runtime ledgers, code intel).
- Location: `src/atelier/infra/`.
- Contains: storage backends, memory bridges, runtime event/cost state, benchmark tooling.
- Depends on: env configuration and optional provider SDKs.
- Used by: core runtime/capabilities and adapters.

## Data Flow

### Primary Request Path

1. HTTP request hits `/v1/reasoning/context` route (`src/atelier/core/service/api.py:2807`).
2. Route constructs runtime façade via `_runtime()` and calls `get_context(...)` (`src/atelier/core/service/api.py:2802`, `src/atelier/core/service/api.py:2814`).
3. `ContextRuntime.get_context` forwards to `AtelierRuntimeCore.get_context` (`src/atelier/gateway/adapters/runtime.py:384`, `src/atelier/core/runtime/engine.py:85`).
4. Runtime pulls ranked reusable procedures via `ContextReuseCapability.retrieve(...)` (`src/atelier/core/runtime/engine.py:100`, `src/atelier/core/capabilities/context_reuse/capability.py:788`).
5. Response is serialized as `ContextResponse` and returned from API (`src/atelier/core/service/api.py:2827`).

### Secondary Flow Name

1. MCP tool `trace` receives run data and normalizes/redacts payload (`src/atelier/gateway/adapters/mcp_server.py:1246`).
2. Adapter validates payload into `Trace` model and writes through runtime store (`src/atelier/gateway/adapters/mcp_server.py:1467`, `src/atelier/gateway/adapters/mcp_server.py:1468`).
3. `ContextStore.record_trace` upserts SQLite row + FTS index and mirrors JSON trace file (`src/atelier/core/foundation/store.py:820`).

**State Management:**
- Durable state is file-backed and SQLite-backed under store roots via `ContextStore` (`src/atelier/core/foundation/store.py:284`).
- Per-session in-memory run state uses append-only ledger objects (`src/atelier/infra/runtime/run_ledger.py:22`).
- MCP adapter keeps process-level caches/singletons (`src/atelier/gateway/adapters/mcp_server.py:152`, `src/atelier/gateway/adapters/mcp_server.py:515`).

## Key Abstractions

**ContextRuntime:**
- Purpose: Gateway-safe façade around core runtime + session lifecycle.
- Examples: `src/atelier/gateway/adapters/runtime.py`.
- Pattern: Façade with context manager session (`run(...)`).

**AtelierRuntimeCore:**
- Purpose: Central capability orchestrator.
- Examples: `src/atelier/core/runtime/engine.py`.
- Pattern: Service object composing many capability collaborators.

**ContextStore:**
- Purpose: Unified persistence API for blocks/traces/rubrics/jobs.
- Examples: `src/atelier/core/foundation/store.py`.
- Pattern: Repository-style persistence class over SQLite + FTS + mirrored files.

**MemoryStore protocol + factory:**
- Purpose: Swap memory backends (`sqlite`, `letta`, `openmemory`) without changing callers.
- Examples: `src/atelier/infra/storage/memory_store.py`, `src/atelier/infra/storage/factory.py`.
- Pattern: Protocol + factory selection.

## Entry Points

**CLI command:**
- Location: `pyproject.toml:85`, `src/atelier/gateway/adapters/cli.py:7902`.
- Triggers: `atelier ...` command.
- Responsibilities: Parse commands/options, invoke runtime/store/capability functions, emit telemetry.

**MCP server command:**
- Location: `pyproject.toml:86`, `src/atelier/gateway/adapters/mcp_server.py:4421`.
- Triggers: `atelier-mcp` under host integration.
- Responsibilities: JSON-RPC stdio loop, tool registry/validation, local or remote dispatch.

**HTTP API app factory:**
- Location: `src/atelier/core/service/api.py:2652`, `src/atelier/core/service/api.py:5769`.
- Triggers: `uvicorn` startup or module import by service process.
- Responsibilities: Build FastAPI app, register routes, enforce auth dependency, expose compatibility and v1 APIs.

## Architectural Constraints

- **Threading:** Predominantly synchronous execution; API lazy store init uses lock-based double-checked init (`src/atelier/core/service/api.py:2677`), MCP spawns background daemon threads for maintenance (`src/atelier/gateway/adapters/mcp_server.py:508`, `src/atelier/gateway/adapters/mcp_server.py:4439`).
- **Global state:** Module-level singletons/caches exist in service and MCP layers (`src/atelier/core/service/config.py:83`, `src/atelier/core/service/api.py:5769`, `src/atelier/gateway/adapters/mcp_server.py:152`, `src/atelier/gateway/adapters/mcp_server.py:515`).
- **Circular imports:** Not detected as hard cycles in inspected files; code relies on deferred in-function imports to reduce coupling and import cost (`src/atelier/core/service/api.py:2802`, `src/atelier/gateway/adapters/runtime.py:162`, `src/atelier/core/service/__init__.py:3`).
- **Persistence model:** `ContextStore` is explicitly single-process/single-writer oriented (`src/atelier/core/foundation/store.py:278`).

## Anti-Patterns

### Mega-adapter modules

**What happens:** Transport adapters accumulate many unrelated concerns in single files (`src/atelier/gateway/adapters/cli.py` ~8719 LOC, `src/atelier/core/service/api.py` ~5793 LOC, `src/atelier/gateway/adapters/mcp_server.py` ~4444 LOC).
**Why it's wrong:** Feature additions increase merge conflicts and make transport concerns leak into domain logic.
**Do this instead:** Add new behavior in capability/runtime modules first (`src/atelier/core/capabilities/*`, `src/atelier/core/runtime/engine.py`) and keep adapters as thin command/route handlers.

### Direct SQL in API routes

**What happens:** Some API handlers execute inline SQL and response shaping in route functions (`src/atelier/core/service/api.py:2718`).
**Why it's wrong:** Storage queries become duplicated transport logic and bypass reusable core abstractions.
**Do this instead:** Route data access through `ContextStore`/capability methods (`src/atelier/core/foundation/store.py`, `src/atelier/core/capabilities/*`) and keep API handlers focused on HTTP mapping.

## Error Handling

**Strategy:** Validate inputs at boundaries; raise protocol-specific errors; suppress non-critical telemetry/background failures.

**Patterns:**
- HTTP handlers raise `HTTPException` for invalid/missing resources (`src/atelier/core/service/api.py:2836`, `src/atelier/core/service/api.py:2863`).
- MCP/CLI wrap non-critical side effects in `contextlib.suppress` and continue (`src/atelier/gateway/adapters/mcp_server.py:1207`, `src/atelier/gateway/adapters/runtime.py:379`).

## Cross-Cutting Concerns

**Logging:** Module loggers and warning/error logs in worker/adapters (`src/atelier/core/service/worker.py:31`, `src/atelier/gateway/adapters/mcp_server.py:49`).
**Validation:** Pydantic models for payload normalization and schema contracts (`src/atelier/core/service/schemas.py`, `src/atelier/gateway/sdk/client.py`, `src/atelier/core/foundation/models.py`).
**Authentication:** FastAPI dependency-based Bearer auth gate (`src/atelier/core/service/auth.py:18`) applied to protected routes (`src/atelier/core/service/api.py:2698`).

---

*Architecture analysis: 2026-05-23*
