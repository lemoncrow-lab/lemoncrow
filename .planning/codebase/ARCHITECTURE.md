<!-- refreshed: 2026-05-28 -->
# Architecture

**Analysis Date:** 2026-05-28

## System Overview

```text
┌──────────────────────────────────────────────────────────────────────────────────┐
│                        Entry Points (Consumer Layer)                             │
├────────────────┬─────────────────┬────────────────┬────────────────┬────────────┤
│  CLI           │  MCP Server     │  FastAPI HTTP  │  SDK Middleware│  Adapters  │
│ `gateway/cli`  │ `adapters/mcp_  │ `core/service/ │  `sdk/`        │ `gateway/  │
│                │  server.py`     │  api.py`        │                │ adapters/` │
└────────┬───────┴────────┬────────┴───────┬────────┴───────┬────────┴─────┬──────┘
         │                │                │                │              │
         └────────────────┴────────────────┴────────────────┴──────────────┘
                                           │
                                           ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                          Gateway Layer                                           │
│  `src/atelier/gateway/`                                                          │
│  HostRegistry, SessionParsers (claude/codex/copilot/cursor/gemini/…),           │
│  Integrations (langfuse, openmemory), SDK clients (Local/MCP/Remote)            │
└───────────────────────────────────────┬──────────────────────────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                           Core Layer                                             │
│  `src/atelier/core/`                                                             │
├───────────────────────────────────────────────────────────────────────────────── │
│  Foundation:  models, store, retriever, renderer, watchdogs, rubric_gate        │
│  `core/foundation/`                                                              │
├───────────────────────────────────────────────────────────────────────────────── │
│  Capabilities:  25+ pluggable modules — context_reuse, context_compression,    │
│                 failure_analysis, loop_detection, quality_router, proof_gate,   │
│                 model_routing, code_context, tool_supervision, memory_arbitra-  │
│                 tion, cross_vendor_routing, cross_vendor_memory, …              │
│  `core/capabilities/<name>/`                                                    │
├───────────────────────────────────────────────────────────────────────────────── │
│  Runtime Orchestrator: AtelierRuntimeCore (wires all capabilities together)     │
│  `core/runtime/engine.py`                                                        │
├───────────────────────────────────────────────────────────────────────────────── │
│  Service:  FastAPI app, session ingest, jobs, telemetry, usage sync             │
│  `core/service/`                                                                  │
└───────────────────────────────────────┬──────────────────────────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                        Infrastructure Layer                                      │
│  `src/atelier/infra/`                                                            │
│  Storage (SQLite/Postgres), MemoryStore (SQLite/Letta/OpenMemory),              │
│  Embeddings, InternalLLM, CodeIntel (SCIP/AST-grep/Zoekt/Tree-sitter/Git),     │
│  Runtime utilities (RunLedger, CostTracker, CheckPoint, SessionReport)          │
└───────────────────────────────────────┬──────────────────────────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                           Persistent Store                                       │
│  ~/.atelier/ (default) — SQLite DBs, Markdown blocks, JSONL traces,             │
│  cost ledger, vector index (lance), memory sidecar                              │
└──────────────────────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| `AtelierRuntimeCore` | Single orchestrator — wires all capabilities, exposes unified API | `src/atelier/core/runtime/engine.py` |
| `ContextStore` | SQLite+FTS5 persistence for ReasonBlocks, Traces, Rubrics, Lessons | `src/atelier/core/foundation/store.py` |
| `ContextRuntime` | In-process runtime adapter for host integrations (context manager API) | `src/atelier/gateway/adapters/runtime.py` |
| MCP Server | JSON-RPC stdio server; exposes all capabilities as MCP tools | `src/atelier/gateway/adapters/mcp_server.py` |
| FastAPI Service | HTTP API for remote clients, dashboard data, session ingestion | `src/atelier/core/service/api.py` |
| CLI (`atelier`) | Click-based CLI — context, traces, rubrics, memory, runtime, telemetry | `src/atelier/gateway/cli/app.py` |
| `AtelierMiddleware` | Drop-in SDK middleware (LangChain/OpenAI/Anthropic/Gemini ADK) | `src/atelier/sdk/middleware.py` |
| Session Parsers | Parse raw host sessions (claude, codex, copilot, cursor, gemini, …) | `src/atelier/gateway/hosts/session_parsers/` |
| Capabilities | 25+ focused modules — each owns one runtime concern | `src/atelier/core/capabilities/<name>/` |
| `RunLedger` | Append-only observable event log for a single agent run | `src/atelier/infra/runtime/run_ledger.py` |
| Storage Factory | Selects SQLite vs Postgres backend at runtime | `src/atelier/infra/storage/factory.py` |
| Code Intel | SCIP indexing, AST-grep, Zoekt search, Tree-sitter, git history | `src/atelier/infra/code_intel/` |
| Retriever | Score/rank ReasonBlocks with BM25 + optional vector cosine | `src/atelier/core/foundation/retriever.py` |
| Foundation Models | Pydantic data contracts: ReasonBlock, Trace, Rubric, TraceLearning, … | `src/atelier/core/foundation/models.py` |
| Frontend | React SPA — dashboards for savings, sessions, memory, routing | `frontend/src/` |

## Pattern Overview

**Overall:** Layered Plugin Architecture with Capability-per-Module decomposition

**Key Characteristics:**
- Each capability (`context_reuse`, `failure_analysis`, `loop_detection`, etc.) is an independent Python module under `core/capabilities/<name>/` with its own `capability.py`, `models.py`, and supporting files.
- `AtelierRuntimeCore` instantiates all capabilities at startup and wires them together — no dynamic discovery at call time.
- All inter-layer contracts are expressed as Pydantic models (`core/foundation/models.py`); the models are the stable API surface.
- Multiple access surfaces (CLI / MCP / HTTP / SDK middleware / adapters) share the same `core/` runtime — no duplicate logic.
- Storage backends are swappable via environment variable (`ATELIER_STORAGE_BACKEND=sqlite|postgres`); memory backends likewise (`ATELIER_MEMORY_BACKEND=sqlite|letta|openmemory`).

## Layers

**Entry Points / Consumer Layer:**
- Purpose: Surface runtime functionality to agent hosts and developers
- Location: `src/atelier/gateway/cli/`, `src/atelier/gateway/adapters/mcp_server.py`, `src/atelier/core/service/api.py`, `src/atelier/sdk/`
- Contains: CLI commands (Click), MCP JSON-RPC server, FastAPI HTTP app, SDK middleware wrappers
- Depends on: Gateway layer, Core layer
- Used by: CLI users, agent hosts (Claude Code, Codex, Copilot, Cursor, Gemini), Python SDK users

**Gateway Layer:**
- Purpose: Translate external host formats and protocols into internal models
- Location: `src/atelier/gateway/`
- Contains: Host session parsers, adapter implementations, SDK clients (LocalClient/MCPClient/RemoteClient), integrations (Langfuse, OpenMemory), HostRegistry
- Depends on: Core foundation models, Infra storage
- Used by: Entry point layer

**Core Layer:**
- Purpose: All reasoning runtime logic — retrieval, compression, failure detection, routing, verification
- Location: `src/atelier/core/`
- Sub-layers:
  - `foundation/` — models, store, retriever, renderer, watchdogs, rubric_gate (baseline primitives)
  - `capabilities/` — 25+ capability modules (each pluggable, independently testable)
  - `runtime/` — `AtelierRuntimeCore` orchestrator
  - `service/` — FastAPI app + background jobs + telemetry
  - `improvement/` — failure analysis and pattern promotion
  - `domains/` — domain-specific ReasonBlock bundles (builtin: `swe.general`)
  - `rubrics/` — rubric definitions and evaluation
- Depends on: Infra layer for storage/embedding/code-intel
- Used by: Gateway layer, entry points

**Infrastructure Layer:**
- Purpose: External I/O — storage, embeddings, LLM calls, code analysis
- Location: `src/atelier/infra/`
- Contains: `storage/` (SQLite/Postgres), `memory_bridges/` (Letta/OpenMemory adapters), `embeddings/` (OpenAI/null), `internal_llm/` (Ollama/OpenAI), `code_intel/` (SCIP/AST-grep/Zoekt/Tree-sitter/git), `runtime/` (RunLedger, CostTracker, CheckPoint, RealtimeContext, SessionReport)
- Depends on: External services (DB, embedding APIs, code index binaries)
- Used by: Core layer

## Data Flow

### Agent Context Request (MCP / CLI)

1. Agent host calls `mcp__atelier__context` or `atelier tools call context` (`src/atelier/gateway/adapters/mcp_server.py`)
2. `ContextRuntime` (or MCP handler) builds a `TaskContext` from task/domain/files/errors
3. `retriever.retrieve()` scores `ReasonBlocks` via BM25 + optional vector similarity (`src/atelier/core/foundation/retriever.py`)
4. `render_context_for_agent()` packs ranked blocks within token budget (`src/atelier/core/foundation/renderer.py`)
5. `ContextResult` returned — context string + token breakdown + recalled passage IDs

### Trace Recording & Failure Learning

1. Agent session ends → host calls `mcp__atelier__record_trace` or `atelier trace add`
2. Trace model validated (`src/atelier/core/foundation/models.py:Trace`)
3. `ContextStore.save_trace()` writes to SQLite + FTS5 mirror (`src/atelier/core/foundation/store.py`)
4. `FailureAnalysisCapability` clusters repeated error signatures (`src/atelier/core/capabilities/failure_analysis/`)
5. `LessonPromoterCapability` proposes promotion to ReasonBlock / Rubric (`src/atelier/core/capabilities/lesson_promotion/`)

### Session Import (Host Sessions → Traces)

1. CLI `atelier import` or API POST → selects correct `SessionParser` for the host
2. Parser normalises raw JSON/JSONL → `Trace` + `RunLedger` entries (`src/atelier/gateway/hosts/session_parsers/<host>.py`)
3. `ingest_session_file()` or `ingest_session_directory()` drives bulk import (`src/atelier/core/service/ingest_session.py`)
4. Parsed traces stored in `ContextStore`; run ledger optionally reconstructed by `LedgerReconstructor`

### Runtime Watchdog Loop (In-process)

1. Agent calls `ContextRuntime.run()` context manager (`src/atelier/gateway/adapters/runtime.py`)
2. `RunLedger` appends events each turn
3. `LoopDetectionCapability` and `ToolSupervisionCapability` evaluate each turn for redundancy/thrashing
4. `WatchdogAlert` emitted → surfaced via `session.watchdogs`
5. On session end: `session.verify()` runs rubric gate; `session.record_trace()` writes outcome

**State Management:**
- Global state: `ContextStore` is a per-process singleton per root path
- Session state: `RunLedger` is per-agent-run; `RealtimeContextManager` tracks context window utilisation
- No shared mutable global between capability instances; each is constructed by `AtelierRuntimeCore`

## Key Abstractions

**ReasonBlock:**
- Purpose: A reusable, reviewable engineering procedure (not hidden memory)
- Examples: `src/atelier/core/foundation/models.py:ReasonBlock`
- Pattern: Pydantic model with tier (`e1`/`e2`/`e3`), triggers, procedure steps, BM25-scored retrieval

**Trace:**
- Purpose: Observable record of one complete agent session (task, tools, errors, outcomes, learnings)
- Examples: `src/atelier/core/foundation/models.py:Trace`
- Pattern: Pydantic model stored as JSON in SQLite + full-text indexed via FTS5

**Capability Module:**
- Purpose: Each capability encapsulates one runtime concern with its own models, logic, and optional capability class
- Examples: `src/atelier/core/capabilities/context_reuse/capability.py`, `src/atelier/core/capabilities/failure_analysis/capability.py`
- Pattern: `class <Name>Capability:` with `__init__(store, ...)` and focused public methods

**ContextStore:**
- Purpose: Unified read/write access to ReasonBlocks, Traces, Rubrics, Lessons
- Examples: `src/atelier/core/foundation/store.py`
- Pattern: Wraps SQLite with FTS5; writes Markdown mirrors to `<root>/blocks/`

**SessionParser:**
- Purpose: Normalise a specific host's raw session format into internal `Trace` + `RunLedger`
- Examples: `src/atelier/gateway/hosts/session_parsers/claude.py`, `codex.py`, `cursor.py`
- Pattern: `parse(raw_path) -> list[Trace]`; subclass `_SessionParser`

## Entry Points

**`atelier` CLI:**
- Location: `src/atelier/gateway/cli/app.py` → `src/atelier/gateway/cli/__main__.py`
- Triggers: `atelier ...` shell command (installed via `pyproject.toml` scripts)
- Responsibilities: Context retrieval, trace management, rubric checks, memory, runtime management, host installation

**`atelier-mcp` MCP Server:**
- Location: `src/atelier/gateway/adapters/mcp_server.py`
- Triggers: Agent host starts process via MCP stdio protocol
- Responsibilities: All core capabilities exposed as JSON-RPC tools; handles `initialize`, `tools/list`, `tools/call`

**FastAPI HTTP Service:**
- Location: `src/atelier/core/service/api.py` (factory: `create_app()`)
- Triggers: `atelier runtime start` / `atelier stack start`
- Responsibilities: REST endpoints for dashboard, session ingestion, savings, memory, host management

**`AtelierMiddleware` SDK:**
- Location: `src/atelier/sdk/middleware.py`
- Triggers: Instantiated by Python code using LangChain/OpenAI/Anthropic/Gemini ADK
- Responsibilities: Inject watchdogs, ledger, loop detection into arbitrary agent frameworks

**Background Controller:**
- Location: Managed by `atelier background start` (systemd/launchd service `atelier-controller.service`)
- Triggers: OS service manager
- Responsibilities: Runs consolidation, lesson promotion, session import background jobs

## Architectural Constraints

- **Threading:** The MCP server is single-process but uses `threading.RLock` within `HostRegistry` and `MemoryStore` for concurrent access safety. The FastAPI service runs with uvicorn and may use multiple workers.
- **Global state:** `ContextStore` is not a module-level singleton — it is constructed per root path, but callers (especially the MCP server) typically hold a single long-lived instance. `TOOLS` dict in `mcp_server.py` is a module-level registry built at import time.
- **Storage root:** Defaults to `~/.atelier`; overridden with `--root` CLI flag or `ATELIER_STORE_ROOT` env var. The entire persistent state lives under this single root directory.
- **No hidden chain-of-thought:** ReasonBlocks, Rubrics, and Traces must be explicit and reviewable. Architecture explicitly forbids opaque memory systems.
- **Lazy imports:** Top-level `__init__.py` and capabilities `__init__.py` use `__getattr__` for lazy module loading to keep import time fast.

## Anti-Patterns

### Bypassing the Foundation Models

**What happens:** Writing raw dicts directly into the SQLite store or reading rows without deserializing via Pydantic
**Why it's wrong:** Breaks forward-compatibility guarantees; field validators and coercions are skipped
**Do this instead:** Always use `ContextStore` methods that return typed Pydantic models (`ReasonBlock`, `Trace`, etc.) from `src/atelier/core/foundation/store.py`

### Adding Logic to `mcp_server.py` or `api.py` Directly

**What happens:** Putting business logic into the MCP handler functions or FastAPI route handlers instead of a capability module
**Why it's wrong:** Logic becomes untestable in isolation; violates the capability-per-module decomposition; duplicates concern across surfaces
**Do this instead:** Create or extend a capability module under `src/atelier/core/capabilities/<name>/capability.py` and call it from the surface handler

### Importing from `infra/` in `core/foundation/`

**What happens:** Foundation models or retriever importing infrastructure adapters (storage, embeddings) directly
**Why it's wrong:** Creates a circular dependency cycle; foundation should be dependency-free except for stdlib and Pydantic
**Do this instead:** Pass the `ContextStore` or embedder as a constructor argument from `core/runtime/engine.py` or the calling layer

## Error Handling

**Strategy:** Exceptions are caught at surface boundaries (CLI, MCP handler, FastAPI route); internal layers propagate typed exceptions or return structured results.

**Patterns:**
- CLI commands use `@click.command` with `try/except` to print human-readable errors
- MCP tools catch all exceptions and return `isError: true` JSON-RPC responses
- FastAPI routes use HTTPException for 4xx/5xx; unexpected exceptions are logged
- Capability methods return `None` or typed result objects; they do not raise into the caller for "empty" cases
- Circuit breaker pattern (`pybreaker`) used in infra adapters that call external services

## Cross-Cutting Concerns

**Logging:** Standard `logging.getLogger(__name__)` throughout; no global logging config — consumers configure their own handlers. Log level controlled via `ATELIER_LOG_LEVEL` or `--verbose` CLI flag.

**Validation:** Pydantic v2 `BaseModel` with `model_config = ConfigDict(extra="forbid")` on all data models. Field validators used extensively in `src/atelier/core/foundation/models.py`.

**Authentication:** Optional Bearer token auth on the FastAPI service (`src/atelier/core/service/auth.py`). MCP server has no auth (stdio transport; trust is at the process level). Controlled via `ATELIER_API_KEY` env var.

**Telemetry:** OpenTelemetry spans + PostHog frontend events. Opt-in/opt-out via `atelier telemetry on|off` or `ATELIER_TELEMETRY=0`. Scrubber (`src/atelier/core/service/telemetry/scrubber.py`) strips PII before emission.

**Redaction:** `src/atelier/core/foundation/redaction.py` strips secrets/tokens from traces before storage.

---

*Architecture analysis: 2026-05-28*
