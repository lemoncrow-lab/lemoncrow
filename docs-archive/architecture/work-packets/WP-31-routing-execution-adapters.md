---
id: WP-31
title: Routing execution adapters and enforcement modes
phase: G
pillar: proof
owner_agent: atelier:code
depends_on: [WP-25, WP-26, WP-27, WP-29]
status: done
---

# WP-31 - Routing Execution Adapters

## Why

The router can decide `cheap`, `mid`, or `premium`, but each host decides model execution
differently. This packet makes routing enforcement explicit so Atelier does not overclaim control
where a host only accepts advice.

## Implementation boundary

- **Host-native:** model selection, model execution, agent/subagent scheduling, chat modes, task
  dispatch, and provider credentials stay owned by the host CLI unless a later provider packet
  explicitly changes that.
- **Atelier augmentation:** this packet returns a serializable `RouteExecutionContract` explaining
  whether the host can enforce, wrap, or only advise on the route decision.
- **Future-only:** `provider_enforced` is a named disabled mode. Do not add provider clients,
  provider secrets, model proxying, or a second agent scheduler here.

## Files touched

- **Edit** `docs/architecture/cost-performance-runtime.md`
- **Edit** `docs/hosts/host-capability-matrix.md`
- **Create** `src/atelier/core/capabilities/quality_router/execution_contract.py`
- **Edit** `src/atelier/core/capabilities/quality_router/capability.py`
- **Edit** `src/atelier/gateway/adapters/cli.py` — add `route contract` command
- **Edit** `src/atelier/gateway/adapters/mcp_server.py` — expose `atelier route contract`
- **Create** `tests/core/test_routing_execution_contract.py`
- **Create** `tests/gateway/test_mcp_route_contract.py`

## How to execute

1. Define execution modes:
   - `advisory`: route decision is returned to the agent; host remains in control.
   - `wrapper_enforced`: an Atelier wrapper can block start, enforce model flags, or stop success
     without verification.
   - `provider_enforced`: Atelier performs the model call through a configured provider adapter.
     This mode must remain disabled until a provider execution packet exists.
2. Implement a serializable `RouteExecutionContract` with:
   - `host`
   - `mode`
   - `supported_tiers`
   - `can_block_start`
   - `can_force_model`
   - `can_require_verification`
   - `fallback_mode`
   - `unsupported_reason`
   - `host_native_owner` for the capability that still belongs to the host, such as `model`,
     `edit`, `compact`, or `agent_orchestration`
3. Wire `atelier route contract(host)` into CLI and MCP.
4. Update routing docs so `RouteDecision` is a decision artifact and `RouteExecutionContract`
   describes whether the host can enforce it.
5. Tests must cover Claude hook enforcement, Codex wrapper enforcement, Copilot advisory mode, and
   provider-enforced disabled-by-default behavior.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/core/test_routing_execution_contract.py \
                     tests/gateway/test_mcp_route_contract.py -v
LOCAL=1 atelier route contract --host codex --json | grep -q "mode"
make verify
```

## Definition of done

- [x] Routing execution modes are documented
- [x] `atelier route contract` returns host-specific enforcement facts
- [x] Provider-enforced mode cannot be selected accidentally
- [x] No provider clients, provider secrets, model proxy, or general agent scheduler added
- [x] Host docs distinguish route decision from route enforcement
- [x] `INDEX.md` updated; trace recorded
