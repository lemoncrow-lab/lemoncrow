# Atelier Execution Summary

> The one-page version for someone joining the project today.

## What we're building

Atelier is the **honest, vendor-neutral layer for AI-assisted coding**. We sit alongside Claude Code, Codex, and Antigravity. We do three things they can't:

1. **Honest cost & quality dashboard** — see where your AI spend goes, including cross-vendor counterfactuals
2. **Cross-vendor memory router** — read and audit memory from all three native CLIs in one inspectable view
3. **Outcome telemetry that learns** — every decision (route, compact) is measured and feeds back

## Why now

The natives all shipped persistent memory in early 2026 (Anthropic Dreaming, Codex consolidation, Gemini Auto Memory). "Memory" is no longer a moat. **Cross-vendor anything** is — because none of them will ever route to a competitor.

The window is 6–12 months before one native partners with another. Sprint.

## The plan

### Phase 1 — 2-week MVP (ship by 2026-05-29)
- Outcome capture (feedback loop foundation) — [Spec 01](../specs/week2/01-outcome-capture.md)
- Per-session cost report — [Spec 02](../specs/week2/02-cost-report.md)
- Read native memories (Claude/Codex/Gemini) — [Spec 03](../specs/week2/03-memory-adapter.md)
- `atelier insights` weekly summary — [Spec 04](../specs/week2/04-insights-command.md)
- Benchmark publication pipeline — [Spec 05](../specs/week2/05-benchmark-publication.md)

### Phase 2 — 30-day wedge (by 2026-06-14)
- Cross-machine sync, counterfactuals, memory audit + rollback, cross-vendor routing, web dashboard MVP

### Phase 3 — 90-day moat (by 2026-08-13)
- Federated outcome learning, Team tier, public leaderboard, integration API

## Business

| Tier | Price | Target customers |
|------|-------|------------------|
| Free | $0 | All local features, single machine |
| Pro | $12/mo | Cross-machine sync, web dashboard |
| Team | $30/user/mo | Shared memory, cost attribution, SSO |
| Enterprise | Custom | On-prem, audit export, SOC2 |

12-month target: **~$13K MRR** — bootstrapped revenue proving the wedge.

## Go-to-market

- Owned content: 1 honest benchmark blog post per week, forever
- Show data, not pitches; trust signal is the moat
- No paid ads. No PR firm. No conference sponsorships in Year 1.

## What we will not do

- Beat native compact on compact alone
- Build a smarter memory algorithm than Anthropic Dreaming
- Custom models or fine-tuning
- IDE plugins before CLI is sticky
- Enterprise sales before Team is repeating

## Documents

- [STRATEGY.md](./STRATEGY.md) — full positioning and moat analysis
- [ROADMAP.md](./ROADMAP.md) — execution-ordered plan
- [PRICING.md](./PRICING.md) — business model and tiers
- [GTM.md](./GTM.md) — distribution and content
- [Execution specs](../specs/README.md) — feature-by-feature briefs for coding agents

## What to do today

If you have 30 minutes: read [STRATEGY.md](./STRATEGY.md).

If you have 2 hours: read all four product docs.

If you're building: pick [Spec 01](../specs/week2/01-outcome-capture.md) — every other spec depends on it.

If you're selling: see [GTM.md](./GTM.md). First task: draft the launch HN post.

If you're sceptical: read [STRATEGY.md](./STRATEGY.md) → "Where the gaps remain" → tell us which gap doesn't hold. We'll revise.
