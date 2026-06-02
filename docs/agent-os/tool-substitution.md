# Tool substitution — MANDATORY

Atelier tools are **not optional wrappers**. They are the reason this repo exists. Using native tools here is eating your own savings.

Shared docs use plain tool names like `context`, `read`, `search`, and `trace`.
Some hosts display those same tools as handles like `mcp__atelier__context` and
`mcp__atelier__read`. When you need the exact callable name, use the one shown by
your host.

| Instead of | Use | Why | Measured |
|---|---|---|---|
| `Read(file)` | `mcp__atelier__read` | outline-first; large files return structure without bytes | **Measured token savings (cl100k_base) from `tests/benchmarks/test_read_ab_real.py`:** Ruby 97.2%, C++ 94.4%, Python 85.6%, Markdown 84.5%, TypeScript 74.0%, Shell 69.9%, Scala 50.6%. Current C/C#/Go/Java/Kotlin/PHP/Rust/Swift fixtures still hit full-read fallback (0%) at their base sizes. |
| `Bash(grep ...)` / `Bash(rg ...)` | `mcp__atelier__grep` | regex/glob/type-filter search with token-budgeted output shaping | **Fixture A/B (`tests/benchmarks/test_search_ab_real.py`):** summary mode 1,908 → 165 tokens (91.4% fewer; 1,743 tokens saved). Tiny regex/glob/context fixtures are near parity (0% saved). |
| manual grep + read + rank workflow | `mcp__atelier__search` | ranked query search plus repo-map construction | **Fixture A/B (`tests/benchmarks/test_search_ab_real.py`):** summary mode 1,908 → 197 tokens (89.7% fewer; 1,711 tokens saved). Tiny regex/glob/context fixtures currently favor native output volume. |
| `mcp__atelier__symbols` | `Bash(grep -rn symbol ...)` | SCIP-indexed; no subprocess | **Fixture A/B (`tests/benchmarks/test_code_search_ab_real.py`):** 2,142 → 432 tokens (79.8% fewer; 1,710 tokens saved) for symbol discovery across a Python module set. || `Bash(anything)` | `mcp__atelier__shell` | ANSI-stripped + line-truncated output | **Fixture A/B (`tests/benchmarks/test_shell_ab_real.py`):** 5,001 → 377 tokens (92.5% fewer; 4,624 tokens saved) on large command output (`seq 1 2000`) with line truncation. |
| `Edit(...)` / `Write(...)` | `mcp__atelier__edit` | atomic multi-file with snapshot/rollback | **Fixture A/B (`tests/benchmarks/test_edit_ab_real.py`):** 74 → 47 tokens (36.5% fewer; 27 tokens saved) for single-file replace response vs manual patch/diff transcript. |
| chaining search + symbol + callers/callees + read | `mcp__atelier__explore` | grouped source + relationships in one budgeted call | **Fixture A/B (`tests/benchmarks/test_code_explore_ab_real.py`):** 3,510 → 1,927 tokens (45.1% fewer; 1,583 tokens saved) for an auth/login exploration flow. |

> Every "X% saved" claim in this row must cite a real A/B test under `tests/benchmarks/`. If the test isn't there, the cell reads `_pending A/B_`. No exceptions.
>
> Run `make bench-ab` to refresh the calibration store at `~/.atelier/savings_calibration.jsonl`.

## Exceptions (native tools are fine)

- `Read` when Atelier MCP is not available / returns `noop`.
- `Edit`/`Write` for tiny single-line fixes where the edit is trivially correct.
- `Bash` for git commands (already auto-allowed, no token overhead).

## Always prefer Atelier MCP tools

Always prefer Atelier MCP tools for file I/O, search, edits, shell commands, and
code intelligence. Native tools are fallback-only.

| Atelier tool | Best for |
|---|---|
| `mcp__atelier__symbols` | Find symbol definitions by name (SCIP-indexed) — faster and exact vs grep |
| `mcp__atelier__node` | Full definition of a specific symbol — faster than `read` for targeted inspection |
| `mcp__atelier__callers` | Who calls a function (inbound call graph) — faster than grep for invocation tracing |
| `mcp__atelier__callees` | What a function calls (outbound call graph) — understand dependencies before editing |
| `mcp__atelier__usages` | All references to a symbol — exact (not textual), unlike grep |
| `mcp__atelier__impact` | Blast radius for a file or symbol — use before refactoring |
| `mcp__atelier__pattern` | AST-structural search / rewrite (ast-grep) — match code shape, not text |
| `mcp__atelier__explore` | Grouped source + relationships in one call — replaces search→node→callers chain |
| `mcp__atelier__grep` | Regex and glob search across files |
| `mcp__atelier__read` | Reading files (outline mode for large files) |
| `mcp__atelier__edit` | Editing files (atomic multi-file with rollback) |
| `mcp__atelier__search` | Ranked semantic search |
| `mcp__atelier__shell` | Shell commands (ANSI-stripped, token-compact output) |

**Decision rules:**

1. **Find a symbol / its definition** → `mcp__atelier__symbols` then `mcp__atelier__node` FIRST (not grep).
2. **Find callers of a function** → `mcp__atelier__callers` FIRST (not grep).
3. **Find what a function calls** → `mcp__atelier__callees` FIRST.
4. **Find all references to a symbol** → `mcp__atelier__usages` FIRST (not grep).
5. **Blast radius before refactoring** → `mcp__atelier__impact` FIRST.
6. **Match or rewrite code by shape** → `mcp__atelier__pattern` FIRST (not regex grep).
7. **Understand a concept across files** → `mcp__atelier__explore` FIRST.
8. **Regex/grep, text search** → `mcp__atelier__grep` FIRST.
9. **File reading** → `mcp__atelier__read` FIRST.
10. **Editing** → `mcp__atelier__edit` FIRST.
11. **Shell commands** → `mcp__atelier__shell` FIRST.

**Fallback:** Use native host tools only when the Atelier equivalent returns `noop`, is hidden, or is unavailable.

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

### Frontend

- Lives in `frontend/`; uses Vite + React.
- `cd frontend && npm run dev` for local dev.
- Docker: `docker-compose up` starts both API and frontend.

### Scripts

- `make install` — full local install via `scripts/install.sh --local`
- `make status` — show install status
- `scripts/sync_agent_context.py` — regenerates AGENTS.md / GEMINI.md / copilot-instructions.md from `docs/agent-os/README.md`; run after editing that file.

## Known behavioural gaps

These are tool behaviours that can confuse an LLM. They are not bugs — they are design boundaries that now have built-in handling in the MCP server code.

### `atelier_read` on a directory — handled (no longer an error)

**Status: ✅ Fixed in server** (`src/atelier/gateway/adapters/mcp_server.py`)

`atelier_read` now detects when the given path is a directory and returns a structured
response with `mode: "directory"`, the list of entries, and a directive for next steps,
instead of raising a cryptic `file not found` error.

### File inventory — use `grep` / `shell`, not a code-intel op

The old `op=files` / `op=routes` / `op=context` / `op=status` ops have been
**retired** from the code-intel tool (calling them now raises a `ValueError`
pointing here). For file inventory, use the focused tools directly:

```
# List files of a given type in a directory (token-budgeted):
mcp__atelier__grep(path="some/dir", output_mode="file_paths_only", file_glob_patterns=["*.yaml"])

# List ALL files (including non-code) in a directory:
mcp__atelier__shell(command="ls path/to/dir")

# Find where a concept lives across files:
mcp__atelier__explore(query="...")
```
