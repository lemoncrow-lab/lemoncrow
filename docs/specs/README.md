# Atelier Execution Specs

> These are the feature specs coding agents and contributors pick up to ship Atelier's roadmap.

Each spec is structured for an autonomous coding agent or contributor to execute end-to-end:

- **Why** — the strategic context (one paragraph)
- **What** — user-visible behaviour and CLI / API surface
- **Where** — exact file paths and module names
- **Data model** — JSON schemas, ledger event shapes
- **Out of scope** — explicit non-goals
- **Acceptance criteria** — testable definition-of-done
- **Open questions** — things the executor must ask before starting

## Phase 1 — 2-week MVP

These ship together as the launch. See [ROADMAP.md](../product/ROADMAP.md) for context.

| Spec | Status |
|------|--------|
| [01-outcome-capture.md](./week2/01-outcome-capture.md) — Feedback loop foundation | Shipped |
| [02-cost-report.md](./week2/02-cost-report.md) — Per-session cost report | Shipped |
| [03-memory-adapter.md](./week2/03-memory-adapter.md) — Read native memories | Shipped |
| [04-insights-command.md](./week2/04-insights-command.md) — `atelier insights` | Shipped |
| [05-benchmark-publication.md](./week2/05-benchmark-publication.md) — Publish pipeline | Shipped |
| [06-web-wiring.md](./week2/06-web-wiring.md) — Surface specs 01–05 in the dashboard | Shipped |

## Phase 2 — 30-day wedge

| Spec | Status |
|------|--------|
| [06-cross-machine-sync.md](./day30/06-cross-machine-sync.md) | Pending |
| [07-counterfactual-report.md](./day30/07-counterfactual-report.md) | Pending |
| [08-memory-audit-viewer.md](./day30/08-memory-audit-viewer.md) | Pending |
| [09-cross-vendor-routing.md](./day30/09-cross-vendor-routing.md) | Pending |
| [10-web-dashboard.md](./day30/10-web-dashboard.md) | Pending |

## Phase 3 — 90-day moat-deepening

| Spec | Status |
|------|--------|
| [11-federated-learning.md](./day90/11-federated-learning.md) | Outline only |
| [12-team-tier.md](./day90/12-team-tier.md) | Outline only |
| [13-public-leaderboard.md](./day90/13-public-leaderboard.md) | Outline only |
| [14-integration-api.md](./day90/14-integration-api.md) | Outline only |

## Cross-phase

These specs span phases and are tracked on their own branches.

| Spec | Status | Branch |
|------|--------|--------|
| [optimization-autopilot.md](./optimization-autopilot.md) — Optimization Advisor (cost vs quality tuning) | Pending review | `feat/optimization-autopilot` |

## How to execute a spec

For a coding agent:

1. Read the spec end-to-end before writing code.
2. Read the **Where** section's referenced files first.
3. Restate scope and acceptance criteria in your own words.
4. Address **Open questions** with the maintainer before implementation.
5. Write tests against the acceptance criteria before the implementation is complete.
6. Update the spec's status section at the bottom when done.

For a contributor:

1. Pick a spec marked **Pending**.
2. Open a GitHub issue: "Implementing [spec name]" — link the spec.
3. Branch off `main`. Branch name: `spec/<spec-number>-<short-name>`.
4. PR description must reference the acceptance criteria and show how each is met.

## Conventions

- **Files**: prefer extending existing modules over creating new ones unless the spec explicitly says otherwise.
- **No new dependencies** without justification in the PR description.
- **Type hints required** on all new functions (Python 3.13+ syntax).
- **Tests required** — unit tests at minimum, integration tests where the spec involves file I/O or subprocess.
- **No emoji in code or docs unless the spec requires them.**
