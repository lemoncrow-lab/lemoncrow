# Codebase Structure

**Analysis Date:** 2026-05-28

## Directory Layout

```
atelier/                            # repo root
├── src/                            # Python source root (PEP 517 src layout)
│   ├── atelier/                    # Main package
│   │   ├── __init__.py             # Public API, lazy-imports all sub-packages
│   │   ├── core/                   # All reasoning runtime logic
│   │   │   ├── foundation/         # Primitive models, store, retriever, renderer, watchdogs
│   │   │   ├── capabilities/       # 25+ pluggable capability modules
│   │   │   │   ├── archival_recall/
│   │   │   │   ├── audit_export/
│   │   │   │   ├── budget_optimizer/
│   │   │   │   ├── code_context/
│   │   │   │   ├── consolidation/
│   │   │   │   ├── context_compression/
│   │   │   │   ├── context_reuse/
│   │   │   │   ├── counterfactual/
│   │   │   │   ├── cross_vendor_memory/
│   │   │   │   ├── cross_vendor_routing/
│   │   │   │   ├── failure_analysis/
│   │   │   │   ├── governance/
│   │   │   │   ├── lesson_promotion/
│   │   │   │   ├── loop_detection/
│   │   │   │   ├── memory_arbitration/
│   │   │   │   ├── model_routing/
│   │   │   │   ├── monitors/
│   │   │   │   ├── optimization/
│   │   │   │   ├── prefix_cache/
│   │   │   │   ├── prompt_compilation/
│   │   │   │   ├── proof_gate/
│   │   │   │   ├── quality_router/
│   │   │   │   ├── registry/
│   │   │   │   ├── repo_map/
│   │   │   │   ├── reporting/
│   │   │   │   ├── semantic_file_memory/
│   │   │   │   ├── style_import/
│   │   │   │   ├── sync/
│   │   │   │   ├── team/
│   │   │   │   ├── telemetry/
│   │   │   │   └── tool_supervision/
│   │   │   ├── runtime/            # AtelierRuntimeCore orchestrator
│   │   │   ├── service/            # FastAPI HTTP service, jobs, telemetry
│   │   │   │   └── telemetry/      # OTel + PostHog exporters
│   │   │   ├── improvement/        # Failure pattern analysis
│   │   │   ├── domains/            # Domain-specific ReasonBlock bundles
│   │   │   │   └── builtin/
│   │   │   │       └── swe.general/
│   │   │   └── rubrics/            # Rubric definitions
│   │   ├── gateway/                # Host/protocol translation layer
│   │   │   ├── adapters/           # MCP server, LangGraph, Aider, Cursor, Hermes, etc.
│   │   │   ├── cli/                # Click CLI app (`atelier` command)
│   │   │   ├── hosts/              # HostRegistry + session parsers
│   │   │   │   ├── session_parsers/  # One parser per agent host
│   │   │   │   └── configs/
│   │   │   ├── integrations/       # Langfuse, OpenMemory, external analytics
│   │   │   └── sdk/                # AtelierClient, LocalClient, MCPClient, RemoteClient
│   │   ├── infra/                  # External I/O, storage, code intelligence
│   │   │   ├── storage/            # SQLite/Postgres store + memory store + vector
│   │   │   │   └── migrations/
│   │   │   ├── memory_bridges/     # Letta and OpenMemory adapters
│   │   │   ├── embeddings/         # OpenAI + null embedder
│   │   │   ├── internal_llm/       # Ollama + OpenAI clients
│   │   │   ├── code_intel/         # SCIP, AST-grep, Zoekt, cross-lang, git history
│   │   │   │   ├── astgrep/
│   │   │   │   ├── cross_lang/
│   │   │   │   ├── git_history/
│   │   │   │   ├── scip/
│   │   │   │   └── zoekt/
│   │   │   ├── runtime/            # RunLedger, CostTracker, Checkpoint, SessionReport
│   │   │   ├── seed_blocks/        # Seed ReasonBlock data
│   │   │   ├── tree_sitter/        # Tree-sitter query layer
│   │   │   └── benchmarks/         # Benchmark runner infrastructure
│   │   └── sdk/                    # Drop-in SDK middleware (LangChain/OpenAI/Anthropic/Gemini)
│   └── benchmarks/                 # Benchmark suites
│       ├── code_intel/             # Code intel benchmarks
│       ├── swe/                    # SWE-bench harness
│       └── tool_bench/             # Tool-level benchmarks
├── tests/                          # Test suite
│   ├── core/                       # Core layer tests (capabilities, foundation, service)
│   │   ├── capabilities/           # Per-capability unit tests
│   │   └── service/                # Service API tests
│   ├── gateway/                    # Gateway/CLI/MCP/adapter tests
│   ├── infra/                      # Infrastructure layer tests
│   ├── benchmarks/                 # Benchmark-level tests
│   ├── fixtures/                   # Shared test fixtures
│   ├── golden/                     # Golden file outputs
│   └── docs/                       # Doc conformance tests
├── frontend/                       # React SPA (Vite + TypeScript)
│   ├── src/
│   │   ├── components/             # UI components
│   │   ├── pages/                  # Page components (Savings, Sessions, Memory, Routing)
│   │   ├── lib/                    # Utilities
│   │   └── test/                   # Frontend tests
│   ├── public/                     # Static assets
│   └── scripts/                    # Frontend build/dev scripts
├── integrations/                   # Per-host integration bundles (install artifacts)
│   ├── claude/                     # Claude Code plugin (AGENTS.md, hooks, skills)
│   ├── codex/                      # Codex CLI integration
│   ├── copilot/                    # GitHub Copilot / VS Code
│   ├── cursor/                     # Cursor IDE
│   ├── copilot-cli/
│   ├── hermes/
│   ├── opencode/
│   ├── antigravity/
│   └── skills/                     # Shared skill bundles for host integrations
├── docs/                           # Current documentation
│   ├── architecture/               # Architecture decision records
│   ├── agent-os/                   # Agent OS guidance files
│   ├── decisions/                  # Decision logs
│   ├── design/                     # Design specs
│   ├── engineering/                # Engineering guides
│   ├── hosts/                      # Per-host integration docs
│   ├── sdk/                        # SDK docs
│   └── specs/                      # Feature specs
├── docs-archive/                   # Archived/superseded documentation
├── examples/                       # Usage examples per host
│   ├── claude/
│   ├── codex/
│   ├── copilot/
│   ├── cursor/
│   ├── gemini/
│   ├── sdk/
│   └── shopify/
├── scripts/                        # Dev/ops shell scripts (install, hooks, sync)
│   ├── hooks/                      # Git hook scripts
│   └── lib/                        # Script utilities
├── deploy/                         # Deployment configs (Letta service)
├── templates/                      # ReasonBlock / rubric templates
│   └── reasonblocks/
├── benchmarks/                     # Benchmark data and outputs (separate from src/)
├── reports/                        # Generated weekly reports
├── artifacts/                      # Build/run artifacts
├── exports/                        # Exported data (e.g. claude session exports)
├── internal/                       # Internal strategy docs
├── pyproject.toml                  # Python project config, deps, entry points
├── Makefile                        # Dev lifecycle targets
├── Dockerfile.api                  # Docker image for API service
├── Dockerfile.frontend             # Docker image for frontend
├── docker-compose.yml              # Stack orchestration
└── uv.lock                         # Locked dependency manifest (uv)
```

## Directory Purposes

**`src/atelier/core/foundation/`:**
- Purpose: Baseline primitives shared by all capabilities and layers
- Contains: Pydantic models (`models.py`), `ContextStore`, `Retriever`, `Renderer`, `Watchdogs`, `RubricGate`, `Extractor`, `Parser`, `Redaction`, `Identity`, path helpers
- Key files: `models.py`, `store.py`, `retriever.py`, `renderer.py`, `watchdogs.py`, `rubric_gate.py`

**`src/atelier/core/capabilities/`:**
- Purpose: Each subdirectory is one self-contained capability module
- Contains: `capability.py` (main class), `models.py` (capability-local models), supporting modules
- Key pattern: `class <Name>Capability:` with focused public methods; registered in `src/atelier/core/capabilities/__init__.py`

**`src/atelier/core/runtime/`:**
- Purpose: Single orchestrator that instantiates and wires all capabilities
- Key files: `engine.py` (`AtelierRuntimeCore`)

**`src/atelier/core/service/`:**
- Purpose: FastAPI application, background job management, telemetry pipeline
- Key files: `api.py`, `config.py`, `jobs.py`, `worker.py`, `schemas.py`, `auth.py`, `telemetry/`

**`src/atelier/gateway/adapters/`:**
- Purpose: Protocol adapters between external surfaces and the core runtime
- Key files: `mcp_server.py` (MCP stdio), `runtime.py` (in-process ContextRuntime), `remote_client.py`, `langgraph_adapter.py`, `cursor_adapter.py`, `hermes_adapter.py`

**`src/atelier/gateway/hosts/session_parsers/`:**
- Purpose: Parse raw session files from each agent host into normalised `Trace` objects
- Key pattern: One file per host (e.g. `claude.py`, `codex.py`, `cursor.py`, `copilot.py`, `gemini.py`); `registry.py` lists `SUPPORTED_SESSION_IMPORT_HOSTS`

**`src/atelier/gateway/sdk/`:**
- Purpose: Public SDK client interfaces
- Key files: `client.py` (abstract `AtelierClient`), `local.py` (`LocalClient`), `mcp.py` (`MCPClient`), `remote.py` (`RemoteClient`)

**`src/atelier/sdk/`:**
- Purpose: Drop-in middleware for Python agent frameworks
- Key files: `middleware.py` (unified entry), `langchain_middleware.py`, `openai_hooks.py`, `anthropic_tools.py`, `gemini_adk.py`

**`src/atelier/infra/storage/`:**
- Purpose: Backend-agnostic storage with pluggable implementations
- Key files: `base.py` (abstract interface), `sqlite_store.py`, `postgres_store.py`, `memory_store.py` (abstract), `sqlite_memory_store.py`, `factory.py`, `vector.py`, `ids.py`

**`src/atelier/infra/code_intel/`:**
- Purpose: Static code analysis infrastructure (symbol lookup, call graphs, search)
- Sub-dirs: `scip/` (SCIP protocol indexer/reader), `astgrep/` (AST-grep adapter + binaries), `zoekt/` (trigram search index), `cross_lang/` (cross-language import resolvers), `git_history/` (blame, renames, graveyard)

**`tests/`:**
- Purpose: Full test suite mirroring `src/` structure
- Mapping: `tests/core/` → `src/atelier/core/`, `tests/gateway/` → `src/atelier/gateway/`, `tests/infra/` → `src/atelier/infra/`

**`integrations/`:**
- Purpose: Generated/curated install artifacts for each agent host
- Contains: AGENTS.md files, skill bundles, plugin configs, hook scripts, MCP config templates
- Not Python packages — these are shipped/installed by `scripts/install.sh`

## Key File Locations

**Entry Points:**
- `src/atelier/gateway/cli/app.py`: Click CLI (`atelier` command), 2000+ lines
- `src/atelier/gateway/cli/__main__.py`: `python -m atelier.gateway.cli` entry
- `src/atelier/gateway/adapters/mcp_server.py`: MCP stdio JSON-RPC server
- `src/atelier/core/service/api.py`: FastAPI app factory (`create_app()`)

**Configuration:**
- `pyproject.toml`: Project config, deps, entry points (`atelier`, `atelier-mcp`)
- `src/atelier/core/service/config.py`: Pydantic-settings config class (`cfg`)
- `src/atelier/core/environment.py`: Environment variable resolution helpers

**Core Logic:**
- `src/atelier/core/foundation/models.py`: All Pydantic data contracts
- `src/atelier/core/foundation/store.py`: `ContextStore` — primary read/write interface
- `src/atelier/core/foundation/retriever.py`: BM25 + vector scoring for ReasonBlocks
- `src/atelier/core/foundation/renderer.py`: Context string assembly for agent injection
- `src/atelier/core/runtime/engine.py`: `AtelierRuntimeCore` orchestrator
- `src/atelier/gateway/adapters/runtime.py`: `ContextRuntime` — in-process session context manager

**Storage:**
- `src/atelier/infra/storage/factory.py`: Backend selection (`create_store`, `make_memory_store`)
- `src/atelier/infra/storage/sqlite_store.py`: Primary SQLite implementation
- `src/atelier/infra/storage/sqlite_memory_store.py`: Memory sidecar SQLite store

**Testing:**
- `tests/core/`: Core capability and foundation tests
- `tests/gateway/`: CLI, MCP, adapter, session-import tests
- `tests/fixtures/`: Shared test fixtures and session samples

## Naming Conventions

**Files:**
- `snake_case.py` for all Python modules
- `capability.py` — main class for each capability module
- `models.py` — Pydantic models local to a module
- `<host>.py` — session parsers named after the host (e.g. `claude.py`, `codex.py`)
- `test_<module>.py` — test files mirror the source module name

**Directories:**
- `snake_case` for all Python packages
- Capability directories named after the capability slug (e.g. `context_reuse`, `failure_analysis`)
- Host session parser files named after the host slug matching `SUPPORTED_SESSION_IMPORT_HOSTS`

**Classes:**
- `<Name>Capability` — capability classes (e.g. `ContextReuseCapability`)
- `<Name>Store` — storage implementations (e.g. `SQLiteStore`, `SqliteMemoryStore`)
- `<Name>Adapter` — gateway adapters (e.g. `LangGraphAdapter`, `CursorAdapter`)
- `<Name>Parser` — session parsers (extend `_SessionParser`)
- `<Name>Client` — SDK clients (e.g. `LocalClient`, `RemoteClient`)

**Constants:**
- `SCREAMING_SNAKE_CASE` for module-level constants (e.g. `PROTOCOL_VERSION`, `CONTEXT_WINDOW_TOKENS`)

## Where to Add New Code

**New Capability:**
- Implementation: `src/atelier/core/capabilities/<name>/capability.py` + `models.py`
- Register in: `src/atelier/core/capabilities/__init__.py` lazy mapping
- Wire in orchestrator: `src/atelier/core/runtime/engine.py` (`AtelierRuntimeCore.__init__`)
- Tests: `tests/core/capabilities/test_<name>.py`

**New Agent Host Session Parser:**
- Implementation: `src/atelier/gateway/hosts/session_parsers/<host>.py` (extend `_SessionParser`)
- Register in: `src/atelier/gateway/hosts/session_parsers/registry.py` (`SUPPORTED_SESSION_IMPORT_HOSTS`)
- Integration artifacts: `integrations/<host>/`
- Tests: `tests/gateway/test_session_parser_<host>.py` or within `test_session_importer_tokens.py`

**New MCP Tool:**
- Add to: `src/atelier/gateway/adapters/mcp_server.py` using `@tool` decorator
- Delegate to: An existing or new capability in `src/atelier/core/capabilities/`
- Tests: `tests/gateway/test_mcp_tool_handlers.py`

**New CLI Command:**
- Add to: `src/atelier/gateway/cli/app.py` using `@click.command` / `@click.group`
- Tests: `tests/gateway/test_cli.py` or `tests/gateway/test_cli_v3_commands.py`

**New Foundation Model:**
- Add to: `src/atelier/core/foundation/models.py`
- Export in: `src/atelier/__init__.py` `_LAZY_EXPORTS`

**New Storage Backend:**
- Implement abstract interface from: `src/atelier/infra/storage/base.py`
- Register in: `src/atelier/infra/storage/factory.py` `create_store()`

**Shared Utilities:**
- Shared helpers (stdlib-only): `src/atelier/core/foundation/` if pure data-model helpers
- Infra utilities: `src/atelier/infra/runtime/` for run-time tracking utilities

## Special Directories

**`.atelier/` (runtime store, not in repo):**
- Purpose: Default persistent store root (`~/.atelier` in production, `.atelier/` in dev)
- Contains: `atelier.db` (SQLite), `blocks/` (Markdown mirrors), `traces/` (JSONL), `raw/` (redacted artifacts), `hosts/`, `lessons/`
- Generated: Yes (at runtime)
- Committed: No

**`.planning/`:**
- Purpose: GSD planning documents
- Contains: `codebase/` analysis docs, phase plans
- Generated: By GSD tooling
- Committed: Yes (planning artifacts)

**`integrations/`:**
- Purpose: Per-host install artifacts (AGENTS.md, skill bundles, MCP config templates, hooks)
- Generated: Partially — some are hand-authored, some generated by `make build-host-skills`
- Committed: Yes

**`docs-archive/`:**
- Purpose: Superseded documentation preserved for reference
- Generated: No
- Committed: Yes (read-only archive)

**`deleted/`:**
- Purpose: Soft-deleted code/files with timestamp prefixes
- Generated: By dev workflow conventions
- Committed: Yes (intentionally)

**`exports/`:**
- Purpose: Exported session data (e.g. Claude session exports for import/testing)
- Generated: Yes (by export commands)
- Committed: Varies

---

*Structure analysis: 2026-05-28*
