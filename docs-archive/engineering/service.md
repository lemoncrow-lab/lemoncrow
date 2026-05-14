# HTTP Service

Atelier includes an optional FastAPI HTTP service. The CLI and stdio MCP server
remain the primary interfaces; use the service when you need remote MCP mode,
the browser dashboard, or a shared multi-agent runtime.

## Starting the Service

Local development without auth:

```bash
ATELIER_REQUIRE_AUTH=false atelier service start --host 127.0.0.1 --port 8787
```

Authenticated service process:

```bash
ATELIER_API_KEY=my-secret-key atelier service start --host 0.0.0.0 --port 8787
```

Inspect the resolved service configuration:

```bash
atelier service config
```

### Key Environment Variables

| Variable                  | Default      | Description                           |
| ------------------------- | ------------ | ------------------------------------- |
| `ATELIER_SERVICE_HOST`    | `127.0.0.1`  | Bind host                             |
| `ATELIER_SERVICE_PORT`    | `8787`       | Bind port                             |
| `ATELIER_REQUIRE_AUTH`    | `true`       | Require `Authorization: Bearer <key>` |
| `ATELIER_API_KEY`         | `""`         | Bearer token value                    |
| `ATELIER_STORAGE_BACKEND` | `sqlite`     | Storage backend                       |
| `ATELIER_DATABASE_URL`    | `""`         | Postgres DSN when using Postgres      |
| `ATELIER_ROOT`            | `~/.atelier` | Runtime store root                    |

## Current Endpoint Groups

### Public

- `GET /health` — liveness check

### System and Compatibility

- `GET /config` — resolved service configuration
- `GET /overview` — compatibility overview payload
- `GET /plans`
- `GET /pricing`
- `GET /ledgers/{session_id}`
- `GET /blocks`
- `GET /blocks/{block_id}`
- `GET /savings`
- `GET /calls`
- `GET /clusters`

### Context and Rubrics

- `POST /v1/context`
- `POST /v1/rescue`
- `GET /v1/rubrics`
- `GET /v1/rubrics/{rubric_id}`
- `POST /v1/rubrics/run`

### Traces

- `GET /traces`
- `GET /v1/traces/{trace_id}`
- `POST /v1/traces`

### Memory and Knowledge

- `GET /v1/memory/blocks`
- `POST /v1/memory/blocks`
- `POST /v1/memory/archive`
- `POST /v1/memory/recall`
- `GET /v1/memory/passages`

### Telemetry and Analytics

- `GET /telemetry/config`
- `POST /telemetry/config`
- `POST /telemetry/ack`
- `GET /telemetry/local`
- `POST /telemetry/local`
- `GET /telemetry/summary`
- `GET /telemetry/schema`
- `GET /analytics`
- `GET /analytics/summary`
- `GET /analytics/dashboard`
- `GET /analytics/external`
- `GET /v1/savings/summary`
- `GET /v1/optimizations/summary`

### Operations

- `GET /mcp/status`
- `GET /hosts`
- `GET /skills`
- `GET /skills/{name}`
- `GET /watchdogs/config`
- `POST /watchdogs/config`

## Authentication

When `ATELIER_REQUIRE_AUTH=true` (default), every endpoint above except
`/health` requires:

```text
Authorization: Bearer <ATELIER_API_KEY>
```

For local development, set `ATELIER_REQUIRE_AUTH=false` before starting the
service.

## Remote MCP Mode

Remote MCP mode forwards the core service-backed flows through this API. The
current coverage includes context retrieval, rescue, rubric execution, trace
recording, and memory operations.

```bash
ATELIER_API_KEY=dev-key atelier service start --host 127.0.0.1 --port 8787

ATELIER_MCP_MODE=remote \
ATELIER_SERVICE_URL=http://localhost:8787 \
ATELIER_API_KEY=dev-key \
atelier-mcp
```

## OpenAPI

When the service is running, FastAPI exposes live API docs at:

```text
http://localhost:8787/docs
```
