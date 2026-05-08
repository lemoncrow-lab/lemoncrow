# MCP Server

Atelier ships a full MCP (Model Context Protocol) server that any compatible AI agent host can use.

## Starting the Server

```bash
uv run atelier-mcp
```

Or with an explicit root:

```bash
uv run atelier-mcp --root /path/to/.atelier
```

The server speaks JSON-RPC 2.0 over stdio. It is designed to be spawned by the agent host process, not run as a persistent daemon.

## Modes

| Mode              | Set via                   | Description                          |
| ----------------- | ------------------------- | ------------------------------------ |
| `local` (default) | `ATELIER_MCP_MODE=local`  | Talks directly to local SQLite store |
| `remote`          | `ATELIER_MCP_MODE=remote` | Forwards calls to the HTTP service   |

Remote mode is used when a team shares a central Atelier instance:

```bash
ATELIER_MCP_MODE=remote \
ATELIER_SERVICE_URL=http://atelier.internal:8787 \
ATELIER_API_KEY=my-key \
uv run atelier-mcp
```

## Tool Registry

### V1 Tools (Core — Always Present)

| Tool        | Description                                            |
| ----------- | ------------------------------------------------------ |
| `reasoning` | Get context prompt for a task (blocks + scoped memory) |
| `lint`      | Validate a plan against dead ends and constraints      |
| `rescue`    | Get rescue procedure for a repeated failure            |
| `verify`    | Run a rubric gate and get pass/blocked result          |
| `trace`     | Record an execution trace                              |
| `search`    | Full-text search across blocks                         |

### V2 Tools (Extended — Always Present)

| Tool                  | Description                                 |
| --------------------- | ------------------------------------------- |
| `reasoning`           | Read per-run state ledger                   |
| `trace`               | Update per-run state ledger                 |
| `trace`               | Emit a monitoring event                     |
| `compact`             | Compress context to reduce token usage      |
| `reasoning`           | Get a reasoning environment by ID           |
| `reasoning`           | Get formatted environment context           |
| `read`                | Read file with smart truncation (FTS-aware) |
| `search`              | Search files with result caching            |
| `search`              | Grep with injection guard and caching       |
| `memory`              | Create or update an editable memory block   |
| `memory`              | Fetch one editable memory block             |
| `memory`              | Archive long-term memory text               |
| `memory`              | Recall relevant archival memory passages    |
| `atelier sql inspect` | Read-only deterministic SQL introspection   |

## Tool Schemas

Tools accept and return JSON. Key patterns:

### `reasoning`

```json
&#123;
  "task": "Wire scoped recall into context injection",
  "domain": "coding",
  "files": ["src/atelier/gateway/adapters/mcp_server.py"],
  "agent_id": "atelier:code",
  "recall": true
&#125;
```

`agent_id` is optional. When it is present and `recall` is not false, the tool recalls up to three
archival passages for that agent and appends them under a `<memory>` section. Recalled passages are
strictly scoped: a passage must belong to the requested `agent_id` or carry the explicit
`agent:any` tag for global lessons.

Returns:

```json
&#123;
  "context": "<reasoning_procedures>...</reasoning_procedures>\n<memory>...</memory>\n",
  "recalled_passages": [&#123;"id": "pas-...", "source": "trace", "score": 0.4&#125;],
  "tokens_breakdown": &#123;"reasonblocks": 180, "memory": 42, "total": 222&#125;
&#125;
```

### `lint`

```json
&#123;
  "task": "Apply a live state change",
  "domain": "state.change",
  "steps": ["Resolve target from URL slug alone", "Apply the update"]
&#125;
```

Returns: `&#123;"status": "blocked"|"pass", "warnings": [...], "suggestions": [...]&#125;`

### `trace`

```json
&#123;
  "agent": "claude-code",
  "domain": "state.change",
  "task": "Apply a live config change",
  "status": "success",
  "commands_run": ["resolve-target", "api.write", "api.read"],
  "errors_seen": [],
  "diff_summary": "Applied change using canonical identifier",
  "output_summary": "Read-after-write verification passed"
&#125;
```

### `verify`

```json
&#123;
  "rubric_id": "rubric_state_change_safety",
  "checks": &#123;
    "canonical_identifier_used": true,
    "pre_change_state_captured": true,
    "read_after_write_completed": true,
    "observed_state_matches_intent": true,
    "rollback_plan_available": true,
    "user_visible_surface_checked": true
  &#125;
&#125;
```

Returns: `&#123;"status": "pass"|"blocked", "failed_checks": [...]&#125;`

### `atelier sql inspect`

```json
&#123;
  "connection_alias": "atelier_local",
  "sql": "SELECT id, title FROM tasks ORDER BY id",
  "params": [],
  "row_limit": 200
&#125;
```

Returns:

```json
&#123;
  "columns": [&#123;"name": "id", "type": "INTEGER"&#125;, &#123;"name": "title", "type": "TEXT"&#125;],
  "rows": [&#123;"id": 1, "title": "First"&#125;],
  "row_count": 1,
  "truncated": false,
  "took_ms": 12
&#125;
```

Alias configuration is loaded from `.atelier/sql_aliases.toml`:

```toml
[aliases.atelier_local]
backend = "sqlite"
env = "ATELIER_LOCAL_SQLITE"
allow_writes = false
```

Only aliases present in this file are reachable. Connection strings should be provided by
environment variables (`env = "..."`) and are never persisted by Atelier.

## Verifying the MCP Server

```bash
# Quick verify (tools/list)
cd atelier && bash scripts/verify_atelier_mcp_stdio.sh

# Manual test
echo '&#123;"jsonrpc":"2.0","method":"tools/list","id":1&#125;' | uv run atelier-mcp
```

Expected: a `tools/list` response containing all V1 + V2 tools.

## Configuration

| Variable                 | Default                      | Description                                   |
| ------------------------ | ---------------------------- | --------------------------------------------- |
| `ATELIER_ROOT`           | `~/.atelier`                 | Trace/history store root                      |
| `ATELIER_KNOWLEDGE_ROOT` | `$WORKSPACE_ROOT/.knowledge` | Optional override for Git-tracked knowledge   |
| `ATELIER_WORKSPACE_ROOT` | `.`                          | Workspace root (for relative path resolution) |
| `ATELIER_MCP_MODE`       | `local`                      | `local` or `remote`                           |
| `ATELIER_SERVICE_URL`    | `http://localhost:8787`      | Remote service URL (when mode=remote)         |
| `ATELIER_API_KEY`        | `""`                         | API key for remote mode                       |
