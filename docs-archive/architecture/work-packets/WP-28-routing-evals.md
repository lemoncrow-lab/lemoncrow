---
id: WP-28
title: Add routing cost-quality evals
phase: F
pillar: routing
owner_agent: atelier:code
depends_on: [WP-25, WP-26, WP-27]
status: done
---

# WP-28 - Routing Cost-Quality Evals

## Why

Routing must be measured by accepted outcomes, not by cheaper calls alone. This packet adds eval
coverage for cheap success rate, premium call rate, escalation success, and cost per accepted patch.

## Files touched

- **Edit** `src/atelier/core/capabilities/budget_optimizer/optimizer.py` only if needed for report input
- **Create** `src/atelier/core/capabilities/quality_router/evals.py`
- **Create** `tests/core/test_routing_evals.py`
- **Edit** `docs/core/benchmarking.md`

## How to execute

1. Import `RoutingEvalSummary` from `src/atelier/core/foundation/routing_models.py`; do not
   redefine report fields outside the data-model doc.
2. Define eval fixtures with route decisions, verifier outcomes, token usage, and acceptance flags.
3. Compute `cost_per_accepted_patch`, `premium_call_rate`, `cheap_success_rate`,
   `escalation_success_rate`, and `routing_regression_rate`.
4. Ensure failed cheap attempts count against cost-quality metrics.
5. Document the metrics in `docs/core/benchmarking.md`.
6. Add tests for successful cheap route, failed cheap route with premium recovery, and regression.

## Acceptance tests

```bash
uv run pytest tests/core/test_routing_evals.py -q
uv run pytest tests/gateway/test_docs.py -q
```

## Definition of done

- [ ] Routing metrics prefer accepted outcomes over raw token savings
- [ ] Failed cheap attempts are visible in reports
- [ ] Benchmarking docs mention routing metrics
- [ ] Acceptance tests pass
- [ ] `trace` called with `WP-28` in `output_summary`
