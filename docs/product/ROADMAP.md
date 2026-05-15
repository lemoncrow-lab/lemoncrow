# Atelier Roadmap

> Last revised 2026-05-15. Cadence: review every 2 weeks.

This roadmap is **execution-ordered**, not aspirational. Each item links to a spec in [`docs/specs/`](../specs/README.md) that a coding agent can pick up.

## Phase 0 — Already shipped (May 2026)

- Per-turn model routing (`ModelRouter`) with 5 scoring axes including session phase
- Dynamic context compaction with LLM hints (task type, risk level, must-keep)
- Workspace state (`~/.atelier/workspaces/`) with session_state.json
- Benchmarks for routing savings, routing quality, compact quality
- Routing replay benchmark using `claude -p` (real haiku counterfactual)
- Telemetry stack: OTel → PostHog + GCP, local-first, opt-out

## Phase 1 — 2-week MVP (ship by 2026-05-29)

The minimum to launch with defensible differentiation. **Everything below ships in one binary, free, open source.**

| # | Feature | Spec | Owner | Acceptance |
|---|---------|------|-------|------------|
| 1 | Outcome capture (feedback loop foundation) | [01-outcome-capture.md](../specs/week2/01-outcome-capture.md) | TBD | Route + compact decisions write outcome windows 5–10 turns later |
| 2 | Honest per-session cost report | [02-cost-report.md](../specs/week2/02-cost-report.md) | TBD | `atelier session report <id>` prints actual vs counterfactual costs |
| 3 | Memory adapter — read Claude / Codex / Gemini | [03-memory-adapter.md](../specs/week2/03-memory-adapter.md) | TBD | `atelier memory list` shows facts from all three vendors |
| 4 | `atelier insights` weekly summary | [04-insights-command.md](../specs/week2/04-insights-command.md) | TBD | Terminal dashboard of spend trends + counterfactuals |
| 5 | Public benchmark publication pipeline | [05-benchmark-publication.md](../specs/week2/05-benchmark-publication.md) | TBD | One-command export to publishable JSON + markdown |

**Definition of done for Phase 1:**
- Launch HN post drafted with screenshots
- `atelier --help` mentions the three pillars
- README has 30-second demo gif
- All five specs have automated tests

## Phase 2 — 30-day wedge (ship by 2026-06-14)

Convert installers into paying users. Sync is the wedge.

| # | Feature | Spec | Acceptance |
|---|---------|------|------------|
| 6 | Cross-machine sync (encrypted) | [06-cross-machine-sync.md](../specs/day30/06-cross-machine-sync.md) | `atelier sync up` / `sync down` works across 2 machines |
| 7 | Counterfactual per-session report | [07-counterfactual-report.md](../specs/day30/07-counterfactual-report.md) | Per-session "what each vendor would have cost" output |
| 8 | Memory audit viewer + rollback | [08-memory-audit-viewer.md](../specs/day30/08-memory-audit-viewer.md) | `atelier memory diff` + `atelier memory rollback` |
| 9 | Cross-vendor live routing | [09-cross-vendor-routing.md](../specs/day30/09-cross-vendor-routing.md) | Router scores Claude vs GPT vs Gemini per turn, not just within Claude |
| 10 | Web dashboard MVP | [10-web-dashboard.md](../specs/day30/10-web-dashboard.md) | atelier.dev/dashboard shows spend trends (Pro tier signup) |

**Definition of done for Phase 2:**
- Pro tier billing live (Stripe)
- 100 paying signups
- 3 published benchmark blog posts
- One enterprise inbound conversation

## Phase 3 — 90-day moat-deepening (ship by 2026-08-13)

Build the things natives structurally can't.

| # | Feature | Spec | Acceptance |
|---|---------|------|------------|
| 11 | Federated outcome learning (opt-in) | [11-federated-learning.md](../specs/day90/11-federated-learning.md) | Anonymised outcomes feed into community routing multipliers |
| 12 | Team tier — shared memory, cost attribution | [12-team-tier.md](../specs/day90/12-team-tier.md) | Team workspace with per-user costs, SSO, RBAC on memory |
| 13 | Public benchmark leaderboard | [13-public-leaderboard.md](../specs/day90/13-public-leaderboard.md) | Weekly auto-refreshed cross-vendor scoreboard |
| 14 | Integration API for tool builders | [14-integration-api.md](../specs/day90/14-integration-api.md) | Documented public API, 1 partner integration live |

**Definition of done for Phase 3:**
- 30 Team customers
- $13K MRR
- Decision point: bootstrap further vs raise seed

## Cross-phase: Optimization Advisor

Tracked on its own branch (`feat/optimization-autopilot`). The feature spans Phase 2 (advisory mode + presets) and Phase 3 (continuous tuning + web Pareto UI).

See [`docs/specs/optimization-autopilot.md`](../specs/optimization-autopilot.md) for the full spec. Key idea: based on the user's last 7 days of real sessions, recommend the cheapest policy that preserves their quality floor. Splits compaction into 4 types (prompt-cache reorder, dedup, retrieval filter, lossy summary) and exposes them as separate savings. Always advisory, never silent.

Phased delivery within the spec:

- **PR-1**: 4-type compaction taxonomy (low-risk refactor, useful standalone)
- **PR-2 → PR-5**: complexity scorer, golden tests, policy presets, `atelier optimize` advisor
- **PR-6**: shadow runner
- **PR-7**: web Pareto UI (depends on Spec 10)

## Phase 4 — Post-90-day options (not committed)

- IDE plugins (VSCode, Cursor, Zed) — only if CLI adoption proves the wedge
- Self-hosted sync server (enterprise) — only after Team tier is repeating
- Mobile companion app — far off, only if user behaviour demands it

## What we explicitly defer

| Idea | Why we wait |
|------|-------------|
| Embedding-based verb classification in router | Speculative, no measurement scaffolding yet |
| New scoring axes for routing beyond session phase | Diminishing returns until feedback loop is live |
| Beating native compact on compact alone | Wrong axis to compete on |
| Custom models or fine-tuning | Need outcome data first |
| Marketplace / app store | Premature; ecosystem first needs an API |

## Risks and contingencies

| If this happens | We do this |
|---|---|
| Anthropic ships a "Claude-and-Codex" memory bridge | Pivot harder into audit + cost (pillars 1, 3) |
| Codex ships cross-machine sync | Lean into vendor-neutral audit + federated learning |
| One native cuts price 50% | Cost pillar weakens; double down on memory + audit |
| Pro tier under-converts | Bundle sync into Free, monetise Team faster |

## Out-of-roadmap requests

Anything not in Phase 1–3 needs explicit strategic justification. Default answer is **no**; we are bandwidth-bound and the moat is finite-time.
