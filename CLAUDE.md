# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

**All Python commands must use `uv run`** — the project uses `uv` for dependency management and there is no activated venv. Direct `python3` calls will fail or use the wrong environment.

```bash
uv run python -c "..."          # one-off Python
uv run pytest ...               # tests
uv run mypy src                 # type-check
uv run atelier ...              # CLI
```

The `.venv` is at `.venv/bin/python3` if you need the path explicitly.

## Common Commands

```bash
# Test
uv run pytest -q                          # all tests (slow tests excluded by default)
uv run pytest -q -x -m "not slow"        # fast, stop on first failure
uv run pytest tests/path/test_file.py -q # single file
uv run pytest -q -k "test_name"          # single test by name

# Lint / format / typecheck
make lint           # ruff
make format         # ruff --fix + black + prettier (frontend)
make typecheck      # mypy --strict src

# Full pre-commit gate
make pre-commit     # format + lint + typecheck + docs + test

# Docs governance
make sync-agent-context   # regenerate host instruction files from docs/agent-os/
make check-agent-context  # verify generated files are up to date

# Install Claude plugin (after changing integrations/claude/plugin/)
bash scripts/install_claude.sh
```

## Architecture

The codebase has three layers with strict dependency direction:

```
gateway/  →  core/  →  infra/
```

- **`src/atelier/gateway/`** — all agent-facing entry points: `cli.py` (the `atelier` CLI), `mcp_server.py` (stdio MCP server for Claude/Codex/Gemini), `runtime.py` (façade for in-process SDK use). Keep entry-point logic thin here.
- **`src/atelier/core/`** — domain logic: `capabilities/` (context reuse, routing, tool supervision, proof gating, semantic memory, code-intel engine), `foundation/` (Pydantic models, SQLite store, paths), `runtime/engine.py` (orchestrator), `service/api.py` (FastAPI HTTP surface).
- **`src/atelier/infra/`** — persistence and integrations: `storage/` (SQLite/Postgres), `runtime/` (run ledger, realtime context), `code_intel/` (SCIP index, ast-grep, Zoekt), `embeddings/`, `memory_bridges/`.

**Key invariant:** New capabilities go in `core/capabilities/`, not in `mcp_server.py` or `cli.py`. Those files are dispatchers only.

## Claude Plugin / Hooks

The Claude Code integration lives in `integrations/claude/plugin/`. After any change:

```bash
bash scripts/install_claude.sh   # stages and reinstalls the plugin
```

Hook scripts run on Claude Code events:
- `hooks/stop.py` — session stats display and auto-record at stop
- `hooks/session_start.py` — session metadata capture
- `hooks/pre_tool_use.py`, `post_tool_use.py` — tool-level savings tracking
- `hooks/session_telemetry.py` — per-tool event emission to `~/.atelier/live_savings_events.jsonl`

Session state is persisted to `~/.atelier/workspaces/<hash>/session_state.json`. Savings for the stop hook come from `~/.atelier/session_stats/<claude-session-uuid>.json`.

## Data / State Layout

All runtime state lives under `~/.atelier/` (or `$ATELIER_ROOT`):

| Path | Contents |
|---|---|
| `runs/<session_id>.json` | Run ledger — events, traces, token stats |
| `session_stats/<uuid>.json` | Per-session savings keyed by Claude Code UUID |
| `live_savings_events.jsonl` | Append-only savings event log (uses internal Atelier session IDs, **not** Claude Code UUIDs) |
| `workspaces/<hash>/session_state.json` | Hook-to-hook state for a workspace |
| `smart_state.json` | Cumulative savings counters |

## Source of Truth Hierarchy

Generated files must never be edited directly — edit the source and regenerate:

| Generated file | Source | Regenerate with |
|---|---|---|
| `AGENTS.md`, `copilot-instructions.md`, host instruction files | `docs/agent-os/*.md` | `make sync-agent-context` |
| Plugin staging dir `~/.atelier/claude-plugin-*/` | `integrations/claude/plugin/` | `bash scripts/install_claude.sh` |

## Coding Guidelines

Behavioral guidelines to reduce common LLM coding mistakes. Bias toward caution over speed; use judgment for trivial tasks.

**1. Think Before Coding** — state assumptions explicitly; if uncertain, ask; if multiple interpretations exist, present them; push back when a simpler approach exists.

**2. Simplicity First** — minimum code that solves the problem; no speculative features, abstractions for single-use code, or error handling for impossible scenarios; if 200 lines could be 50, rewrite it.

**3. Surgical Changes** — touch only what you must; don't improve adjacent code, refactor things that aren't broken, or delete unrelated dead code; match existing style; remove only the imports/variables/functions that *your* changes made unused.

**4. Goal-Driven Execution** — transform tasks into verifiable goals before implementing; for multi-step work, state a brief plan with per-step verify checks; loop until verified.

See [docs/agent-os/coding-guidelines.md](docs/agent-os/coding-guidelines.md) for the full reference.

## Validation by Change Surface

| What changed | Minimum check |
|---|---|
| Python runtime / CLI | `make lint && make typecheck && make test` |
| Hook scripts (`integrations/claude/plugin/hooks/`) | `python3 -m py_compile <file>` then reinstall and smoke-test |
| MCP tool handlers | `uv run pytest tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py -q` |
| Code-intel engine | `uv run pytest tests/core/test_code_context.py -q && make lint && make typecheck` |
| Frontend | `cd frontend && npm run build && npm run test` |
| Docs / host instruction sources | `make docs-check && make check-agent-context` |

## Code Intelligence

For all code-intel needs (symbol search, definitions, callers, callees, impact,
file tree, routes, context), use `mcp__atelier__code` — it handles all of them
with SCIP-indexed precision and native project awareness.
