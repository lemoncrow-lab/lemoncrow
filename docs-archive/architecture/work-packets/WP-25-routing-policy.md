---
id: WP-25
title: Implement quality-aware routing policy configuration
phase: F
pillar: routing
owner_agent: atelier:code
depends_on: [WP-02, WP-14]
status: done
---

# WP-25 - Routing Policy Configuration

## Why

Atelier needs coding-specific routing policy before it can choose cheap, mid, or premium model
tiers. Provider routers optimize infrastructure. WP-02 owns the Pydantic models and DDL; this packet
implements the local default policy and configuration loader.

## Files touched

- **Create** `src/atelier/core/capabilities/quality_router/__init__.py`
- **Create** `src/atelier/core/capabilities/quality_router/policy.py`
- **Create** `src/atelier/core/capabilities/quality_router/config.py`
- **Create** `tests/core/test_routing_policy.py`
- **Edit** `src/atelier/core/capabilities/__init__.py` if exports are used locally

## How to execute

1. Import `AgentRequest`, `ContextBudgetPolicy`, and `RouteDecision` from
   `src/atelier/core/foundation/routing_models.py`. Do not redefine fields.
2. Implement `RoutingPolicyConfig` loaded from `.atelier/routing.toml` with sane defaults when the
   file is missing.
3. Keep model/provider names as strings loaded from config; do not hard-code vendor prices.
4. Add protected-file patterns, high-risk domain patterns, route thresholds, and verifier
   requirements to the config.
5. Implement pure policy functions that map request + budget + evidence summary to a draft
   `RouteDecision`.
6. Add focused tests for default policy values, protected-file matching, high-risk escalation, and
   provider-neutral config loading.

## Acceptance tests

```bash
uv run pytest tests/core/test_routing_policy.py -q
```

## Definition of done

- [ ] Routing policy config loads with and without `.atelier/routing.toml`
- [ ] No provider prices or model names are hard-coded into policy defaults
- [ ] High-risk and protected-file rules force premium/escalation
- [ ] Acceptance tests pass
- [ ] `trace` called with `WP-25` in `output_summary`
