# Atelier Public Benchmarks

## What This Is

A reproducible public benchmarking system that proves Atelier's value through honest A/B comparisons: Atelier-on vs Atelier-off, measuring cost, latency, and quality together — never cost alone. Runs against canonical harnesses (TerminalBench) and against any developer's own GitHub PRs, with raw transcripts published and losses reported.

## Core Value

A stranger can clone the repo, run one command, and reproduce the exact benchmark results we published — including the losses.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] **BENCH-01**: `ATELIER_BENCH_MODE` env var cleanly toggles Atelier-on vs Atelier-off (no routing, compaction, memory, or tool substitution in off mode)
- [ ] **BENCH-02**: TerminalBench adapter runs pinned 10-task subset and captures transcript, tokens, latency, cost, and grader verdict per run
- [ ] **BENCH-03**: A/B runner executes N≥5 replications per cell with seeded determinism and produces `summary.json` with mean + 95% CI
- [ ] **BENCH-04**: Report generator produces 3 delta plots (cost, latency, quality) and a `report.md` with methodology, headline table, per-task transcript links, and an explicit losses section
- [ ] **BENCH-05**: External publication pipeline assembles a self-contained blog post directory (index.md, transcripts/, plots/, reproduce.sh) consumable by the existing docusaurus site
- [ ] **BENCH-06**: Long-session quality-degradation suite (50/100/200 turns) with recall-rubric grader, answering the "does Atelier lose context?" objection
- [ ] **BENCH-07**: `atelier bench run` CLI with `--quick` (1 task, N=2, <5 min) and `--full` (10 tasks, N=5) modes, printing a terminal comparison table
- [ ] **BENCH-08**: `atelier bench run --pr <url>` replays any GitHub PR twice (Atelier-on and Atelier-off), scores diff quality against the real merge, reports cost + latency + quality delta

### Out of Scope

- Cost-only benchmarks (no quality signal) — violates non-negotiable rule #1; misleading without quality
- Internal weekly snapshots only (existing `publisher.py`) — not externally reproducible, wrong shape for devs
- Vendored copy of TerminalBench — must be a pinned submodule or PyPI dep for reproducibility
- Benchmarks that hide losses — every published run must include a losses section even if empty

## Context

- Atelier went public on 2026-05-26. Within 48 hours, 20+ developers asked for benchmarks unprompted.
- Existing `benchmarks/mcp_tools/` covers tool-level token deltas on synthetic cases — not end-to-end quality.
- Existing `benchmarks/swe/atelier_proxy.py` runs SWE-bench predictions but lacks an explicit off-arm and publication shape.
- Existing `src/atelier/infra/benchmarks/publisher.py` produces internal weekly snapshots — not externally reproducible.
- Target: benchmark report #1 published at `docs-site/blog/2026-06-04-*` covering TerminalBench × Claude Sonnet × 10 tasks × N=5.
- PR-replay benchmarks (`--pr <url>`) let any developer run a personal A/B on their own real work.

## Constraints

- **Quality**: Three metrics always together — cost ($), latency (s), quality (pass/fail or grader score)
- **Transparency**: Raw transcripts published, losses published, N≥5 runs with 95% CI
- **Reproducibility**: Every published report includes exact CLI command and commit SHA to reproduce
- **Timeline**: First published report by 2026-06-04 (D1–D5 are the critical path)
- **Tech stack**: Python, existing `benchmarks/mcp_tools/` harness patterns, tiktoken, matplotlib; TerminalBench as submodule or PyPI dep

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Use `claude -p` subprocess under TerminalBench | Matches existing `benchmarks/swe/atelier_proxy.py` pattern; more credible ("used Anthropic's own CLI") | — Pending |
| Wilson score interval for pass-rate CI | Binary metric — normal approximation is wrong at low N | — Pending |
| PR-replay scores diff quality against real merge | Real tasks with ground-truth; complete benchmark cell (base commit + prompt + expected output) | — Pending |
| Reuse `ANTHROPIC_API_KEY`, print which key is used | Simpler UX; add `--no-cost-cap` override at $50 default hard-stop | — Pending |
| All together as one milestone (D1–D7 + PR-replay) | User explicitly confirmed scope | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-05-28 after initialization*
