# Security

## Threat Model

Atelier handles agent-generated content (plans, traces, errors, diff summaries) and stores it in a local or shared database. The primary risks are:

1. **Secret leakage** — agent output may accidentally contain tokens, API keys, or credentials
2. **Prompt injection** — adversarial content in traces or blocks could influence future agent runs
3. **Shell injection** — `cached-grep` takes user-supplied patterns that could execute arbitrary commands
4. **Unauthorized access** — HTTP service must be protected in multi-user environments
5. **Database misuse** — ad-hoc SQL could mutate data or exceed bounded query behavior

## Controls

### Secret Redaction

All trace string fields (`task`, `diff_summary`, `output_summary`, `files_touched`, `commands_run`, `errors_seen`) pass through a redaction filter before persistence. The filter removes:

- Common secret patterns (API keys, tokens, Bearer values)
- Environment variable assignments with secret-looking names
- Credentials embedded in URLs

This is implemented in `src/atelier/core/redaction.py` and applied in:

- `atelier trace record` CLI command
- `trace` MCP tool
- `POST /v1/traces` HTTP endpoint
- `POST /v1/traces/&#123;id&#125;/finish` HTTP endpoint

### No Chain-of-Thought Storage

Atelier never stores:

- Agent reasoning steps
- Hidden thought content
- Intermediate planning notes

Only observable fields are stored: what commands ran, what errors appeared, what the diff contained.

### Hooks Disabled by Default

The `integrations/claude/plugin/hooks/` directory contains lifecycle hooks that intercept agent tool calls. These are **disabled by default** in `integrations/claude/plugin/hooks/hooks.json`.

Hook state is persisted at `$&#123;workspace&#125;/.atelier/session_state.json`. It contains operational session metadata such as active session identifiers and loop-detection state. No hidden reasoning content is stored.

### OpenMemory Stub

The OpenMemory bridge (`src/atelier/integrations/openmemory.py`) is a no-op stub by default. It becomes active only when `ATELIER_OPENMEMORY_ENABLED=true`. Without this flag, all OpenMemory calls return empty results with a notice. There is no accidental data leakage to an external memory service.

### Cached-Grep Injection Guard

`search` and `cached-grep` CLI validate grep patterns before execution:

- Patterns are not passed to shell via string interpolation
- `subprocess` is called with an argument list, never `shell=True`
- Suspicious metacharacters in patterns are rejected

### HTTP Service Authentication

When `ATELIER_REQUIRE_AUTH=true` (default), all `/v1/*` requests must carry:

```text
Authorization: Bearer <ATELIER_API_KEY>
```

For development only, `ATELIER_REQUIRE_AUTH=false` disables auth entirely. Do not use this in production or shared environments.

The service binds to `127.0.0.1` by default (not `0.0.0.0`). Change only when explicitly deploying for remote access.

### No Secret Storage

Atelier never writes `ATELIER_API_KEY`, OpenAI keys, Shopify tokens, or any other credentials to the store. Environment variables are read at startup only.

### SQL Inspect Alias Allowlist + Read-only Gate

The MCP `sql` tool and CLI `atelier sql inspect` only connect to aliases declared in
`.atelier/sql_aliases.toml` under `[aliases.*]`.

- Only listed aliases are reachable.
- Connection strings should be supplied through environment variables (`env = "..."`).
- SQL statements are single-statement and read-only by default.
- Write statements (DML/DDL) are rejected unless that alias sets `allow_writes = true`.
- Query runtime is bounded per backend: Postgres uses `SET LOCAL statement_timeout = '5s'`;
  SQLite uses `PRAGMA busy_timeout=5000`.

This keeps SQL introspection deterministic and bounded while preventing accidental write paths in
the default configuration.

## OWASP Considerations

| OWASP Category                | Status                                                                   |
| ----------------------------- | ------------------------------------------------------------------------ |
| A01 Broken Access Control     | HTTP service has API key auth; binds localhost by default                |
| A02 Cryptographic Failures    | No cryptography used; secrets handled by env vars only                   |
| A03 Injection                 | Cached-grep uses argument list (not shell=True); patterns validated      |
| A04 Insecure Design           | Chain-of-thought excluded by design; only observable data stored         |
| A05 Security Misconfiguration | REQUIRE_AUTH=true by default; service disabled by default                |
| A06 Vulnerable Components     | `make verify` includes security checks; `uv` manages deps                |
| A07 Auth Failures             | API key required by default; no session tokens or JWTs                   |
| A08 Software Integrity        | `uv.lock` pins all dependencies                                          |
| A09 Logging Failures          | Audit log appended for every mutation; no truncation                     |
| A10 SSRF                      | No outbound HTTP from the store layer; MCP server only reads local store |

## Security Testing

```bash
cd atelier && make security-test
```

This runs security-focused test cases including redaction validation and injection guard tests.
