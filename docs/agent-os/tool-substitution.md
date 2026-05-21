# Tool substitution — MANDATORY

Atelier tools are **not optional wrappers**. They are the reason this repo exists. Using native tools here is eating your own savings.

| Instead of | Use | Why | Measured |
|---|---|---|---|
| `Read(file)` | `mcp__atelier__read` | outline-first; large files return structure without bytes | **88.7% chars saved (median)** on 4 large repo files — see `tests/benchmarks/test_read_ab_real.py` |
| `Bash(grep ...)` / `Bash(rg ...)` | `mcp__atelier__search` | ranked + dedup + range slicing in one call | _pending A/B — see [plan](../plans/active/savings-honest-ab/README.md)_ |
| `Bash(grep ...)` for symbols | `mcp__atelier__code op=search` | SCIP-indexed; no subprocess | _pending A/B_ |
| `Bash(anything)` | `mcp__atelier__shell` | ANSI-stripped + line-truncated output | _pending A/B_ |
| `Edit(...)` / `Write(...)` | `mcp__atelier__edit` | atomic multi-file with snapshot/rollback | _pending A/B_ |
| raw file reading for symbols | `mcp__atelier__code op=symbol/usages/hover` | precomputed graph | _pending A/B_ |

> Every "X% saved" claim in this row must cite a real A/B test under `tests/benchmarks/`. If the test isn't there, the cell reads `_pending A/B_`. No exceptions.
>
> Run `make bench-ab` to refresh the calibration store at `~/.atelier/savings_calibration.jsonl`.

## Exceptions (native tools are fine)

- `Read` when Atelier MCP is not available / returns `noop`.
- `Edit`/`Write` for tiny single-line fixes where the edit is trivially correct.
- `Bash` for git commands (already auto-allowed, no token overhead).

## Model routing — use cheap sub-agents for read work

Before spawning an `Agent(...)` for a read-only task, call `mcp__atelier__route` to get the recommended model tier. Read/search/explore tasks should spawn as `Agent(model="haiku")`. Edit/implement tasks stay on the current model.

```
# exploration, file reading, grep work
Agent(model="haiku", description="...", prompt="...")

# implementation, editing, multi-file changes
Agent(...)  # current model
```

**Cross-vendor routing** (`route.yaml` at `~/.atelier/route.yaml`) is configured for Anthropic only until `OPENAI_API_KEY` / `GOOGLE_API_KEY` are set. Token-efficiency savings (`mcp__atelier__read`, `mcp__atelier__search`) work regardless of vendor.

## Tooling conventions

### Python

- **Always use `uv run` instead of bare `python` or `python3`** — the project uses `uv` for environment management.
  - `uv run pytest tests/ -x -q`
  - `uv run python -c "import atelier; ..."`
  - NOT `python -m pytest` (wrong venv)
  - NOT `python3 src/...` (not in project env)
- Source lives under `src/`; Python path is `["src", "."]` (set in `pyproject.toml`).
- Run tests: `uv run pytest tests/ -x -q --tb=short`
- Type-check: `uv run mypy src/atelier/`
- Lint/format: `uv run ruff check src/` and `uv run ruff format src/`

### Package management

- Add deps: `uv add <package>` (updates `pyproject.toml` + `uv.lock`)
- Add optional dep to an extra: edit `pyproject.toml` `[project.optional-dependencies]` manually, then `uv sync`
- Install extras: `uv sync --extra rename` (example)
- Never use `pip install` directly inside this repo.

### MCP server

- Entry point: `uv run atelier-mcp` (registered script in `pyproject.toml`)
- Dev server with reload: `uv run uvicorn atelier.gateway.adapters.http_api:app --reload`
- All MCP tools are registered in `src/atelier/gateway/adapters/mcp_server.py` via `@mcp_tool(name=...)`.
- **Do NOT add new top-level `@mcp_tool` entries** unless a milestone file explicitly says so; add new `op=` values to existing tools instead.

### Frontend

- Lives in `frontend/`; uses Vite + React.
- `cd frontend && npm run dev` for local dev.
- Docker: `docker-compose up` starts both API and frontend.

### Scripts

- `make install` — full local install via `scripts/install.sh --local`
- `make status` — show install status
- `scripts/sync_agent_context.py` — regenerates AGENTS.md / GEMINI.md / copilot-instructions.md from `docs/agent-os/README.md`; run after editing that file.
