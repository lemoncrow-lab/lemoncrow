---
id: WP-26
title: Add quality-aware router runtime integration
phase: F
pillar: routing
owner_agent: atelier:code
depends_on: [WP-25, WP-14]
status: done
---

# WP-26 - Routing Runtime Integration

## Why

The router must sit inside `AtelierRuntimeCore` so CLI, MCP, SDK, and service paths share one
decision surface. Host adapters should not implement their own routing logic.

## Files touched

- **Create** `src/atelier/core/capabilities/quality_router/capability.py`
- **Edit** `src/atelier/core/capabilities/__init__.py`
- **Edit** `src/atelier/core/runtime/engine.py`
- **Edit** `src/atelier/gateway/adapters/mcp_server.py` — register `route`
- **Edit** `src/atelier/gateway/adapters/cli.py` — add `route decide` command
- **Create** `tests/core/test_quality_router.py`
- **Create** `tests/gateway/test_mcp_route_decide.py`

## How to execute

1. Implement a deterministic first-pass router that uses WP-25 policy functions to map request
   risk, step type, memory confidence, verifier coverage, and previous failures to
   `deterministic`, `cheap`, `mid`, or `premium`.
2. Use existing runtime evidence: ReasonBlocks retrieved, files touched, errors seen, ledger loop
   signals, and context budget data.
3. Return a structured `RouteDecision`; do not call external model providers in this packet.
4. Add runtime methods that expose the router through MCP/CLI without host-specific routing logic.
5. Add tests for low-risk cheap routing, high-risk premium routing, repeated-failure escalation, and
   MCP tool registration.

## Acceptance tests

```bash
uv run pytest tests/core/test_quality_router.py tests/gateway/test_mcp_route_decide.py -q
```

## Definition of done

- [ ] Router capability is registered with runtime core
- [ ] `route` is registered in MCP and mirrored by CLI
- [ ] Decisions are deterministic and trace-friendly
- [ ] Repeated failure or protected files force premium/escalation
- [ ] Acceptance tests pass
- [ ] `trace` called with `WP-26` in `output_summary`
