# External Integrations

**Analysis Date:** 2026-05-28

## LLM Providers

**Primary Routing Layer:**

- LiteLLM (`litellm >=1.83.14`) — used for pricing/cost calculation only (`src/atelier/core/capabilities/pricing.py`); NOT used as the call proxy
- Backend selection via `ATELIER_LLM_BACKEND` env var (default: `"ollama"`)

**OpenAI / Compatible APIs:**

- SDK: `openai >=1.0` (optional extra `atelier[cloud]`)
- Client: `src/atelier/infra/internal_llm/openai_client.py`
- Auth: `ATELIER_OPENAI_API_KEY` → falls back to `OPENAI_API_KEY`
- Base URL: `ATELIER_OPENAI_BASE_URL` (supports any OpenAI-compatible endpoint)
- Default model: `gpt-4o-mini` (override via `ATELIER_OPENAI_MODEL`)
- Embeddings: `src/atelier/infra/embeddings/openai_embedder.py` — uses `OPENAI_API_KEY`

**Ollama (Local LLM):**

- SDK: `ollama >=0.6.2` (core dep), also `ollama >=0.4.0` in `smart` extra
- Client: `src/atelier/infra/internal_llm/ollama_client.py`
- No API key required; connects to local Ollama daemon
- Default backend when `ATELIER_LLM_BACKEND` is not set

## Data Storage

**SQLite (Default):**

- Engine: Python stdlib `sqlite3` — no external service
- Primary store: `src/atelier/infra/storage/sqlite_store.py`
- Memory store: `src/atelier/infra/storage/sqlite_memory_store.py`
- Vector embedding cache: `src/atelier/infra/storage/vector.py` (SQLite-backed)
- Location: `$ATELIER_ROOT/` (default: `~/.atelier/`)

**PostgreSQL (Optional):**

- Enabled via: `ATELIER_STORAGE_BACKEND=postgres` + `ATELIER_DATABASE_URL`
- Connection string format: `postgresql://user:pass@host:5432/db`
- Driver: `psycopg[binary] >=3.1` (optional extra `atelier[postgres]`)
- ORM: SQLAlchemy >=2.0.49 for schema management
- Store: `src/atelier/infra/storage/postgres_store.py` (lazy import guard)
- Migrations: `src/atelier/infra/storage/migrations/`
- 15 production tables created by `store.init()`
- Tests skip when `ATELIER_DATABASE_URL` is not set

**pgvector (Optional):**

- Enabled with `atelier[vector]` extra
- Packages: `pgvector >=0.2`, `numpy >=1.26`
- Script: `src/atelier/infra/storage/migrations/` (postgres_vector_script)
- Used for semantic similarity search in PostgreSQL

**Storage Factory:**

- `src/atelier/infra/storage/factory.py` — selects backend (sqlite / postgres / letta / openmemory)

## Memory & Knowledge Sidecars

**Letta (Optional Persistent Memory):**

- Enabled via: `ATELIER_LETTA_URL` set in environment
- SDK: `letta-client >=1.7.12` (extra `atelier[memory]`) or `letta >=0.16.7` (extra `atelier[memory-server]`)
- Adapter: `src/atelier/infra/memory_bridges/letta_adapter.py`
- URL: `ATELIER_LETTA_URL` (default: `http://localhost:8283`)
- Auth: `ATELIER_LETTA_API_KEY`
- Embedder: `src/atelier/infra/embeddings/letta_embedder.py`
- Purpose: long-term memory blocks and archival passage storage

**mem0 / OpenMemory (Optional):**

- SDK: REST API calls to a locally-running OpenMemory server
- Bridge: `src/atelier/gateway/integrations/openmemory.py`, `src/atelier/infra/memory_bridges/openmemory.py`
- URL: `ATELIER_OPENMEMORY_URL` (default: `http://127.0.0.1:8765`)
- User ID: `ATELIER_OPENMEMORY_USER_ID` (falls back to `$USER`)
- OpenAI key required for embeddings: `ATELIER_OPENMEMORY_OPENAI_API_KEY` → falls back to `OPENAI_API_KEY`
- Repo source: `ATELIER_OPENMEMORY_REPO_URL` (default: `https://github.com/mem0ai/mem0.git`)

## Embeddings

**Selection logic (`src/atelier/infra/embeddings/factory.py`):**

1. Pin via `ATELIER_EMBEDDER` env var or API arg: `local` | `openai` | `letta` | `null`
2. Letta sidecar available → `LettaEmbedder`
3. `OPENAI_API_KEY` set → `OpenAIEmbedder`
4. Default: `LocalEmbedder` (no external service)

**Embedders:**

- `src/atelier/infra/embeddings/local.py` — local in-process embeddings
- `src/atelier/infra/embeddings/openai_embedder.py` — OpenAI embeddings API
- `src/atelier/infra/embeddings/letta_embedder.py` — delegates to Letta
- `src/atelier/infra/embeddings/null_embedder.py` — no-op (testing)

## Observability & Analytics

**OpenTelemetry (Tracing):**

- Packages: `opentelemetry-api/sdk/exporter-otlp-proto-http >=1.27`
- Config: `src/atelier/core/service/telemetry/config.py`
- Endpoint: `ATELIER_OTEL_ENDPOINT` (default: `http://localhost:4318`)
- Export format: OTLP HTTP/protobuf
- OTel Collector config: `deploy/otel-collector.yaml`
- Collector scrubs PII fields (`cwd`, `file_path`, `repo_url`, `prompt`, `code`) before forwarding

**PostHog (Product Analytics):**

- Backend: `ATELIER_POSTHOG_KEY` + `ATELIER_POSTHOG_HOST` (default: `https://us.i.posthog.com`)
- Frontend: `posthog-js ^1.150.0` in `frontend/package.json`
- OTLP ingest: OTel Collector can forward directly to PostHog OTLP endpoint
- Telemetry opt-out: `ATELIER_TELEMETRY=0` or `~/.config/atelier/telemetry.toml`
- Auto-disabled during pytest unless `ATELIER_TELEMETRY_ALLOW_IN_TESTS=1`

**Prometheus (Metrics):**

- Package: `prometheus-client >=0.21`
- Exposes metrics endpoint on the FastAPI service

**Langfuse (Optional LLM Tracing):**

- Integration: `src/atelier/gateway/integrations/langfuse.py`
- Enabled via: `ATELIER_LANGFUSE_ENABLED=true`
- Auth: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`
- Host: `LANGFUSE_HOST` (default: `https://cloud.langfuse.com`)
- Fail-open design: errors silently swallowed, never interrupts agent loop
- SDK: `langfuse` package (optional, not in pyproject.toml deps — must be installed separately)

**GCP Logging (Optional):**

- Via OTel Collector `googlecloud` exporter in `deploy/otel-collector.yaml`
- Config: `GCP_PROJECT_ID` env var on the collector container
- Log name: `atelier`

#### Agent Host Integrations

**MCP (Model Context Protocol) Server:**

- Entry: `src/atelier/gateway/adapters/mcp_server.py` (primary gateway, ~5000+ lines)
- CLI entry point: `atelier-mcp` script
- Mode: `ATELIER_MCP_MODE=local` (in-process) or `remote` (HTTP proxy to service)
- Remote: `ATELIER_SERVICE_URL` (default: `http://127.0.0.1:8787`)

**Supported Agent Hosts (session parsers + adapters):**

| Host            | Session Parser                                               | Adapter                                               |
| --------------- | ------------------------------------------------------------ | ----------------------------------------------------- |
| Claude Code     | `src/atelier/gateway/hosts/session_parsers/claude.py`      | —                                                    |
| OpenCode        | `src/atelier/gateway/hosts/session_parsers/opencode.py`    | —                                                    |
| Cursor IDE      | `src/atelier/gateway/hosts/session_parsers/cursor.py`      | `src/atelier/gateway/adapters/cursor_adapter.py`    |
| GitHub Copilot  | —                                                           | `src/atelier/gateway/adapters/remote_client.py`     |
| Cline (VS Code) | `src/atelier/gateway/hosts/session_parsers/cline.py`       | —                                                    |
| Roo Code        | `src/atelier/gateway/hosts/session_parsers/roo_code.py`    | —                                                    |
| Aider           | —                                                           | `src/atelier/gateway/adapters/aider_adapter.py`     |
| Continue        | —                                                           | `src/atelier/gateway/adapters/continue_adapter.py`  |
| SWE-agent       | —                                                           | `src/atelier/gateway/adapters/sweagent_adapter.py`  |
| OpenHands       | —                                                           | `src/atelier/gateway/adapters/openhands_adapter.py` |
| LangGraph       | —                                                           | `src/atelier/gateway/adapters/langgraph_adapter.py` |
| Hermes          | `src/atelier/gateway/hosts/session_parsers/pi.py`          | `src/atelier/gateway/adapters/hermes_adapter.py`    |
| Codex CLI       | `src/atelier/gateway/hosts/session_parsers/codex.py`       | —                                                    |
| Gemini          | `src/atelier/gateway/hosts/session_parsers/gemini.py`      | —                                                    |
| Goose           | `src/atelier/gateway/hosts/session_parsers/goose.py`       | —                                                    |
| Antigravity     | `src/atelier/gateway/hosts/session_parsers/antigravity.py` | —                                                    |
| Kiro            | `src/atelier/gateway/hosts/session_parsers/kiro.py`        | —                                                    |
| Qwen            | `src/atelier/gateway/hosts/session_parsers/qwen.py`        | —                                                    |

**Integration config bundles** (MCP JSON, host-specific instructions):

- Location: `integrations/` directory — per-host subdirs (`claude/`, `cursor/`, `copilot/`, `opencode/`, `hermes/`, etc.)
- Skills bundles: `integrations/skills/`

## Code Intelligence Tools

**tree-sitter (Built-in):**

- Packages: `tree-sitter >=0.23`, `tree-sitter-language-pack >=1.8.1`
- Wrapper: `src/atelier/infra/tree_sitter/`
- Used for: AST tag extraction, language detection, symbol analysis, repo maps

**Zoekt (Code Search — External Binary):**

- Type: External Go binary (not a Python package)
- Adapter: `src/atelier/infra/code_intel/zoekt/`
- Binary discovery: `src/atelier/infra/code_intel/zoekt/binary.py`
- Server wrapper: `src/atelier/infra/code_intel/zoekt/server.py`
- Used for: fast full-text code search via `smart_search`

**ast-grep (Code Transform — External Binary):**

- Type: External binary
- Adapter: `src/atelier/infra/code_intel/astgrep/`
- Used for: structural code search and rename operations

**SCIP Protocol:**

- Type: Protobuf-based code intelligence protocol
- Adapter: `src/atelier/infra/code_intel/scip/`
- Used for: cross-language symbol resolution

**Git Integration:**

- `GitPython >=3.1.50` — high-level Git operations
- `pygit2 ==1.19.2` — low-level git history, blame, rename tracking
- Adapters: `src/atelier/infra/code_intel/git_history/`

## Authentication & Identity

**Service Auth:**

- Type: Bearer API key
- Config: `ATELIER_API_KEY` (expected key value), `ATELIER_REQUIRE_AUTH` (default: `false`)
- Implementation: `src/atelier/core/service/auth.py`
- Auth disabled by default for local/dev use

**External Auth:**

- No OAuth or third-party auth provider
- GitHub: `GITHUB_TOKEN` used only for lesson PR bot feature (`ATELIER_LESSON_PR_BOT_ENABLED`)

## Webhooks & Callbacks

**Incoming:**

- MCP protocol over stdio (local mode) — `src/atelier/gateway/adapters/mcp_server.py`
- REST API on port 8787 — FastAPI endpoints in `src/atelier/core/service/api.py`

**Outgoing:**

- GitHub API calls (lesson PR bot, `GITHUB_TOKEN` required)
- OpenAI API calls (`ATELIER_OPENAI_BASE_URL` / `OPENAI_API_KEY`)
- Ollama daemon calls (local HTTP to Ollama server)
- Letta sidecar calls (`ATELIER_LETTA_URL`)
- OpenMemory calls (`ATELIER_OPENMEMORY_URL`)
- OTLP telemetry export (`ATELIER_OTEL_ENDPOINT`)
- PostHog analytics (backend via OTel, frontend via posthog-js)
- Langfuse traces (`LANGFUSE_HOST`)

## External CLI Tools (Optional Binaries)

**tokscale:**

- Type: External CLI binary
- Env: `ATELIER_TOKSCALE_BIN` or auto-discovered as `tokscale` on `$PATH`
- Integration: `src/atelier/gateway/integrations/external_analytics.py`
- Purpose: Token scale analysis reporting
- License: MIT

## Environment Configuration Summary

**Required for basic operation (SQLite + Ollama):**

- None — works out of the box with local filesystem and Ollama daemon

**Required for cloud LLM:**

- `OPENAI_API_KEY` or `ATELIER_OPENAI_API_KEY`
- `ATELIER_LLM_BACKEND=openai`

**Required for PostgreSQL:**

- `ATELIER_STORAGE_BACKEND=postgres`
- `ATELIER_DATABASE_URL=postgresql://user:pass@host/db`

**Required for production service:**

- `ATELIER_SERVICE_ENABLED=true`
- `ATELIER_REQUIRE_AUTH=true`
- `ATELIER_API_KEY=<secret>`

**Required for telemetry:**

- `ATELIER_OTEL_ENDPOINT=<otlp-http-endpoint>`
- `ATELIER_POSTHOG_KEY=<project-key>` (optional PostHog direct)

**Example production env:** `.env.production.example` (exists at repo root — see file for all keys)

---

*Integration audit: 2026-05-28*
