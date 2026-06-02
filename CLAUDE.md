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

| Path                                   | Contents                                                                                    |
| -------------------------------------- | ------------------------------------------------------------------------------------------- |
| `runs/<session_id>.json`               | Run ledger — events, traces, token stats                                                    |
| `session_stats/<uuid>.json`            | Per-session savings keyed by Claude Code UUID                                               |
| `live_savings_events.jsonl`            | Append-only savings event log (uses internal Atelier session IDs,**not** Claude Code UUIDs) |
| `workspaces/<hash>/session_state.json` | Hook-to-hook state for a workspace                                                          |
| `smart_state.json`                     | Cumulative savings counters                                                                 |

## Source of Truth Hierarchy

Generated files must never be edited directly — edit the source and regenerate:

| Generated file                                                 | Source                        | Regenerate with                  |
| -------------------------------------------------------------- | ----------------------------- | -------------------------------- |
| `AGENTS.md`, `copilot-instructions.md`, host instruction files | `docs/agent-os/*.md`          | `make sync-agent-context`        |
| Plugin staging dir `~/.atelier/claude-plugin-*/`               | `integrations/claude/plugin/` | `bash scripts/install_claude.sh` |

## Coding Guidelines

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## Validation by Change Surface

| What changed                                       | Minimum check                                                                                    |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Python runtime / CLI                               | `make lint && make typecheck && make test`                                                       |
| Hook scripts (`integrations/claude/plugin/hooks/`) | `python3 -m py_compile <file>` then reinstall and smoke-test                                     |
| MCP tool handlers                                  | `uv run pytest tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py -q` |
| Code-intel engine                                  | `uv run pytest tests/core/test_code_context.py -q && make lint && make typecheck`                |
| Frontend                                           | `cd frontend && npm run build && npm run test`                                                   |
| Docs / host instruction sources                    | `make docs-check && make check-agent-context`                                                    |

## Agent Spawning Rules

When spawning sub-agents via the `Agent` tool, always pick the narrowest type:

| Role                                                     | subagent_type     | When                                                    |
| -------------------------------------------------------- | ----------------- | ------------------------------------------------------- |
| Code-review**finder** (read, search, grep — never edits) | `atelier:explore` | All Phase 1 / Angle A–G finder agents in `/code-review` |
| Code-review**verifier** (applies rubric, never edits)    | `atelier:review`  | All Phase 2 verifier agents in `/code-review`           |
| Read-only research / exploration                         | `atelier:explore` | Any agent that only reads files, symbols, or web pages  |
| Coding, edits, fixes                                     | `atelier:code`    | Any agent that writes or modifies files                 |
| Repeated failure / rescue                                | `atelier:repair`  | When the same approach fails twice                      |

**Never** use the default (`claude`) agent for a task that fits one of the typed roles above — the default has write access it doesn't need and costs more.

## Code Intelligence

Use the dedicated, focused code-intel tools (SCIP-indexed, prefer over `grep`):

| Need                                       | Tool                                              |
| ------------------------------------------ | ------------------------------------------------- |
| Find a symbol definition by name           | `mcp__atelier__symbols`                           |
| Read the full source of one symbol         | `mcp__atelier__node`                              |
| Who calls a function / what it calls       | `mcp__atelier__callers` / `mcp__atelier__callees` |
| All references to a symbol                 | `mcp__atelier__usages`                            |
| Blast radius before refactoring            | `mcp__atelier__impact`                            |
| Match/rewrite code by AST shape            | `mcp__atelier__pattern`                           |
| Grouped source + relationships in one call | `mcp__atelier__explore`                           |

There is no `mcp__atelier__code` tool — it was split into the focused tools
above for discoverability. The multiplexer is still registered as
`mcp__atelier__symbols` (its `op=` parameter is an internal detail).
