# Layers

Atelier is organized around a small set of durable layers:

| Layer | Purpose | Allowed dependencies |
| --- | --- | --- |
| `core/` | domain models, rules, capability logic, service contracts | standard library and lower-level utility helpers |
| `infra/` | storage, runtime plumbing, persistence, adapters to local state | `core/` |
| `gateway/` | CLI, MCP, host integrations, service wiring | `core/`, `infra/` |
| `frontend/` | dashboard UI and browser-side client logic | service API contracts, frontend-local utilities |
| `scripts/` | operational glue, generation, verification, install helpers | repository files, stable CLI commands |

## Direction rules

- `core` must not depend on `gateway`.
- `infra` should not depend on `gateway`.
- Host-specific behavior belongs under `gateway/hosts` or `integrations/`.
- Repository policy and agent workflow live in `docs/agent-os/`, not in random comments.

## Source of truth rules

- API request and response contracts live in the Python service schemas and API modules.
- Frontend API behavior must be verified against `frontend/src/api.ts`.
- Install behavior must match `scripts/install_*.sh` and the host docs under `docs/hosts/`.
