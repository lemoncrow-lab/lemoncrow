---
id: WP-23
title: Promote read-only `sql inspect` to MCP tool (live SQL introspection)
phase: C
pillar: 3
owner_agent: atelier:code
depends_on: []
status: done
---

# WP-23 — SQL introspection MCP tool

## Why

Replace the `psql → bash → parse → repeat` chain with a single deterministic
SQL tool. The CLI subcommand `atelier sql inspect` already exists; we surface it as an MCP tool
and harden it.

## Implementation boundary

- **Host-native:** interactive database shells, migrations, write workflows, credentials, tunnels,
  and production access policies stay owned by the host environment and existing DB tooling.
- **Atelier augmentation:** `atelier sql inspect` provides bounded, read-only, deterministic query
  results for reasoning and trace evidence.
- **Not in scope:** do not build an interactive SQL client, schema migration system, data editor, or
  broad database administration surface.

## Files touched

- `src/atelier/gateway/adapters/mcp_server.py` — edit (register `atelier sql inspect`)
- `src/atelier/core/capabilities/tool_supervision/sql_inspect.py` — likely exists, edit
- `tests/gateway/test_sql_inspect_mcp.py`
- `docs/engineering/mcp.md` — edit
- `docs/engineering/security.md` — edit (allowlist policy)

## How to execute

1. Input: `&#123; connection_alias, sql, [params], [row_limit=200] &#125;`
2. Output:

   ```json
   &#123;
     "columns": [&#123;"name": "id", "type": "TEXT"&#125;, ...],
     "rows": [&#123;...&#125;, ...],
     "row_count": 17,
     "truncated": false,
     "took_ms": 23
   &#125;
   ```

3. Connection aliases configured in `.atelier/sql_aliases.toml`. Only aliases listed there are
   reachable. Connection strings are read from environment variables — never persisted.

4. **Read-only by default.** Reject DML/DDL unless `connection_alias` has
   `allow_writes=true` set in the alias file. This is the security gate.
   The V2 default should ship with no write-enabled aliases.

5. Always wrap the query in `SET LOCAL statement_timeout = '5s'` (Postgres) or `PRAGMA
busy_timeout=5000` (SQLite).

6. Output deterministically truncated to `row_limit`; signal `truncated=true`.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/gateway/test_sql_inspect_mcp.py -v

# Smoke: read-only must reject INSERT
LOCAL=1 atelier sql inspect --alias atelier_local "INSERT INTO foo VALUES(1)" || echo "rejected as expected"

make verify
```

## Definition of done

- [x] MCP tool registered
- [x] Read-only enforcement covered by test
- [x] No interactive DB client or migration/write workflow added
- [x] Statement-timeout enforced for both backends
- [x] Connection aliases never logged or persisted
- [x] `make verify` green
- [x] `INDEX.md` updated; trace recorded
