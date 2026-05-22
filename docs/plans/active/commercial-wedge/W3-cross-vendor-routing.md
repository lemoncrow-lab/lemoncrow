# W3 — Cross-vendor live routing surface

> Parent: [`index.md`](index.md). Driving spec: [`day30/09-cross-vendor-routing.md`](../../../specs/day30/09-cross-vendor-routing.md).
> Consumes: existing within-vendor `ModelRouter`, `outcome_capture.py`, and W2's pricing + capability tables.

## Goal

Extend the existing single-vendor `ModelRouter` so that, given a turn's
tool + phase + budget, it can recommend a model from **any configured
vendor**, not just the active one. Surface the recommendation through MCP
so the host CLI sees it transparently. The user installs once, configures
vendor keys once, and the agent picks the right model per turn.

Critically: **W3 ships the recommendation surface, not the in-process model
hand-off.** A vendor model swap mid-session would require host CLI buy-in
that does not exist yet. W3 routes the decision; the host either honours it
(if it can) or logs it as a counterfactual the next session report shows.

## Why now

- W2 sells the saving as a static report. W3 makes the saving real-time.
- Cannot land before W2: users have to see the savings post-hoc before they
  trust a router making the call live.
- Should land before W4 (sync) only if the founder wants the demo loop
  ("install, configure, get cheaper") in the marketing flow. Otherwise W4
  goes first per the index ordering. **Default: W4 first**, then W3.

## Where the code lives

```
src/atelier/core/capabilities/cross_vendor_routing/
  __init__.py                NEW package
  configuration.py           NEW — vendor enablement + key presence checks
  router.py                  NEW — extends the within-vendor scoring to cross-vendor
  advisor.py                 NEW — MCP-facing surface; emits route recommendations
  policy.py                  NEW — risk class, downgrade allowances, hard pins
src/atelier/gateway/adapters/mcp_server.py
                             EXTEND — `route` tool gains `op="recommend"` that returns the advisor's pick
src/atelier/gateway/adapters/cli.py
                             EXTEND — `route configure`, `route plan`, `route status`
tests/core/capabilities/cross_vendor_routing/
  test_advisor_returns_cheapest_capable.py
  test_advisor_respects_phase_pin.py
  test_recommendation_is_logged_as_counterfactual_when_unused.py
```

## Inputs

- **Within-vendor router** — existing scoring (haiku/sonnet/opus) stays
  unchanged; the new router calls into it once per enabled vendor and picks
  across the returned set.
- **Pricing + capability tables** — reused from W2 (`counterfactual/pricing.py`,
  `capabilities.py`). One source of truth.
- **Outcome capture** — `outcome_capture.py` is the feedback signal; W5
  later closes the loop on these decisions.

## Configuration

```
$ atelier route configure
Detected API keys via env / config:
  [x] Anthropic           (ANTHROPIC_API_KEY)
  [x] OpenAI              (OPENAI_API_KEY)
  [x] Google Gemini       (GOOGLE_API_KEY)
  [ ] AWS Bedrock         (not configured)
Enable cross-vendor routing? (y/n) y

Defaults (editable):
  edit turns:   stay on actual vendor's high-tier
  read turns:   any vendor, cheapest capable
  agent turns:  any vendor with tool-use, capability-matched
  risk class:   low

Saved to ~/.atelier/route.yaml
```

`route.yaml` is the only piece of user-editable state W3 owns. It is synced
by W4 like everything else.

## Decision flow per turn

1. Read tool, phase, expected tokens, required capabilities.
2. For each enabled vendor, ask the within-vendor router for its pick.
3. Filter by capability (W2 table). Drop infeasible.
4. Apply `policy.py`: edit/agent pins, downgrade allowances, hard pins.
5. Rank by predicted cost (W2 pricing) × predicted-quality prior (from
   `outcome_capture` history when available; otherwise neutral).
6. Emit a `RouteRecommendation` over MCP with `(vendor, model, reason,
   predicted_cost, fallback)`.
7. Whether or not the host honours it, **log the recommendation alongside
   the actual decision**. That is the feedback W5 trains on, and the data
   the next session counterfactual shows the user.

## User-visible CLI

```
$ atelier route plan --tool Read --task "find the failing test"
Recommendation: gemini-flash
  reason: read tool, exploration phase (turn 2), bounded task
  estimated cost: $0.002 vs $0.01 on sonnet (-80%)
  fallback: claude-haiku-4.6

$ atelier route status
  Vendors enabled: anthropic, openai, google
  Today: 142 turns
    routed-by-host:    96
    advisory-only:     46  (host honoured 34, declined 12)
  Estimated savings vs single-vendor sonnet: $4.22
```

## Validation

- `test_advisor_returns_cheapest_capable` — given a tool/phase, the advisor
  picks the cheapest capable model across vendors.
- `test_advisor_respects_phase_pin` — `edit` phase is pinned to the actual
  vendor's high-tier unless the user opts into downgrade.
- `test_advisor_skips_unconfigured_vendor` — vendor without an API key is
  invisible to the router even if it would be cheapest.
- `test_recommendation_is_logged_as_counterfactual_when_unused` — a
  recommendation the host did not honour shows in the next session report.
- `test_outcome_capture_feeds_quality_prior` — fixture history reshapes the
  ranking; absence of history is neutral.
- `test_route_yaml_is_round_trippable` — config write → read → write is
  identity.

## Exit criteria

- `route configure / plan / status` work end-to-end.
- MCP `route` tool surfaces `op="recommend"` returning a deterministic
  shape.
- Within-vendor router behaviour is unchanged when only one vendor is
  configured (regression-tested).
- Pricing + capability tables are not duplicated from W2.
- Advisory-only mode records every recommendation as a comparable
  counterfactual.
- Trace recorded via `mcp__atelier__record`.

## Out of scope (this milestone)

- **In-process model hand-off mid-session.** Needs host CLI cooperation;
  the design space for that lives in a follow-up spec.
- **Latency-aware routing.** First version optimises cost-given-capability;
  latency is a future weight.
- **Per-team routing policies.** W6.
- **Network probes** to test vendor liveness. Treat configured-with-key as
  available; surface failures through outcome capture, not pre-flight.

## Open questions

1. **Advisory vs binding.** Should we offer a `--binding` flag the user
   sets when they trust the router enough to fail closed if the host
   declines? Default: **no in v1**; W3 is strictly advisory + logged.
2. **Quality prior staleness.** Outcome history older than N days — should
   it decay? Default: **30-day half-life**, override per vendor.
3. **Where do we surface the recommendation in the host CLI?** MCP tool
   call result is the default. A status-line hook is a v1.1 nicety.
