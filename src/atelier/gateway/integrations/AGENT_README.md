# Gateway Integrations

## Purpose

Keep lightweight integration helpers used by gateway adapters and runtime wiring.

## Entry Points

- `openmemory.py` — OpenMemory bridge with local persistence and optional remote sync.
- `ledger_reconstructor.py` — rebuild helper logic for runtime ledgers.
- `_session_parser.py` — shared session parsing utilities.
- `langfuse.py` — Optional Langfuse observability integration for trace recording.

## Langfuse Integration

Enabled by setting `ATELIER_LANGFUSE_ENABLED=1` (or `true`/`yes`) plus `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`. Optionally set `LANGFUSE_HOST` (defaults to `https://cloud.langfuse.com`).

- `emit_trace(payload)` — emits an `atelier.{domain}` trace to Langfuse. Fail-open: any error is silently swallowed so the core loop is never broken.
- `health_check()` — returns a diagnostic dict for health endpoints.
- Hooked into `trace` in `mcp_server.py` after the local ledger write.

## OpenMemory Contract

- Local bridge is always active (no separate enable toggle — set `ATELIER_MEMORY_BACKEND=openmemory` to activate it).
- SQLite-backed, stores in the existing `atelier.db` (no separate file).
- Remote sync is best-effort: MCP client is created lazily and never blocks local persistence.
- Public functions return stable payloads with `ok`, `action`, and `data`.

## Where To Look Next

- `src/atelier/infra/memory_bridges/openmemory.py`
- `src/atelier/gateway/cli/app.py`
- `src/atelier/gateway/adapters/mcp_server.py`
