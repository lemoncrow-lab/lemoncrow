# Atelier Public Benchmarks

## Current Milestone: v0.4 Dedicated Language Support

**Goal:** Give every recognized language first-class code intelligence through canonical language identity, dedicated tree-sitter structure, expanded repo-map tags, and runtime-provisioned SCIP indexing.

**Target features:**
- Canonical Language Registry: one source of truth for extensions, parser names, tags, and SCIP metadata
- Dedicated Tree-sitter Outlines: shell, YAML, TOML, JSON, and SQL leave the generic regex path where grammar and savings justify it
- Tree-sitter Repo-map Tags: all tree-sitter languages can contribute definition tags to PageRank repo maps
- Expanded SCIP Registry: Go, Rust, Java, Ruby, C, and C++ join Python/TypeScript/JavaScript semantic indexing
- Runtime SCIP Provisioning: Atelier-managed install and lazy bootstrap paths make indexers discoverable without manual PATH setup
- Validation and Docs: fixture matrix, honest savings benchmark, SCIP availability report, and docs reflect shipped behavior

## What This Is

A reproducible public benchmarking system that proves Atelier's value through honest A/B comparisons: Atelier-on vs Atelier-off, measuring cost, latency, and quality together — never cost alone. Runs against canonical harnesses (TerminalBench) and against any developer's own GitHub PRs, with raw transcripts published and losses reported.

## Core Value

A stranger can clone the repo, run one command, and reproduce the exact benchmark results we published — including the losses.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] **DLS-LANG**: Code-intel language identity is centralized in a canonical registry used by extension detection, tree-sitter outlines, repo-map tags, and SCIP binaries
- [ ] **DLS-OUTLINE**: Shell, YAML, TOML, JSON, and SQL use dedicated tree-sitter outlines when parser support and the existing savings guard make them better than generic
- [ ] **DLS-TAGS**: Repo-map symbol tags are tree-sitter-derived for every tree-sitter language, while Python AST tags and regex fallback remain intact
- [ ] **DLS-SCIP**: SCIP semantic indexing covers Go, Rust, Java, Ruby, C, and C++ with env overrides, argv templates, lazy execution, and cache outputs
- [ ] **DLS-PROV**: SCIP indexers are installed or bootstrapped from Atelier-managed runtime locations instead of relying only on system PATH
- [ ] **DLS-VAL**: Per-language fixtures, honest savings benchmarks, availability reporting, and docs prove and explain the expanded language support

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
- v0.2 Phase 8 shipped Context Lineage: commit summaries are searchable alongside code chunks and M1 benchmark scaffolding exists.
- v0.3 is driven by the design docs in `docs/plans/context-quality-lift/` and `docs/plans/phase-linear-cache-reuse/`; do not re-decide those architecture choices during planning.
- v0.4 is driven by the design docs in `docs/plans/dedicated-language-support/`; use those milestone files as the source of truth.

## Constraints

- **Quality**: Three metrics always together — cost ($), latency (s), quality (pass/fail or grader score)
- **Transparency**: Raw transcripts published, losses published, N≥5 runs with 95% CI
- **Reproducibility**: Every published report includes exact CLI command and commit SHA to reproduce
- **Timeline**: First published report by 2026-06-04 (D1–D5 are the critical path)
- **Tech stack**: Python, existing `benchmarks/mcp_tools/` harness patterns, tiktoken, matplotlib; TerminalBench as submodule or PyPI dep
- **v0.3 proof target**: local benchmark evidence must show lower cost and lower latency with equal-or-better task success; TerminalBench-oriented target is ≥90% pass rate
- **Language support source of truth**: canonical language names must be shared across extension detection, tree-sitter outlines, repo-map tags, and SCIP registry
- **Runtime provisioning**: cheap SCIP indexers can be installed by Atelier; heavy toolchain-backed indexers should be detected and documented rather than force-installed

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Use `claude -p` subprocess under TerminalBench | Matches existing `benchmarks/swe/atelier_proxy.py` pattern; more credible ("used Anthropic's own CLI") | — Pending |
| Wilson score interval for pass-rate CI | Binary metric — normal approximation is wrong at low N | — Pending |
| PR-replay scores diff quality against real merge | Real tasks with ground-truth; complete benchmark cell (base commit + prompt + expected output) | — Pending |
| Reuse `ANTHROPIC_API_KEY`, print which key is used | Simpler UX; add `--no-cost-cap` override at $50 default hard-stop | — Pending |
| All together as one milestone (D1–D7 + PR-replay) | User explicitly confirmed scope | — Pending |
| Phase-linear cache reuse before broader agent execution | Survey→Plan cache warmth and minified reads are the highest-leverage cost/latency lever before final proof benchmarks | — v0.3 active |
| Counterexamples in tool-result channel only | Preserves static/system prompt cache stability and makes failures actionable without cache-busting prompt mutation | — v0.3 active |
| Scoped pull as the default context gradient | Subtask-scoped context is required to reduce over-fetch while preserving recall for implementation agents | — v0.3 active |
| Canonical language names follow tree-sitter parser names | Parser loading is the hard constraint and fixes shell/bash drift at the source | — v0.4 active |
| SCIP provisioning is tiered | Python/TypeScript are cheap install-time wins; Go/Ruby/Clang can lazy-fetch; Rust/Java depend on heavier user toolchains | — v0.4 active |

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
*Last updated: 2026-05-29 — Milestone v0.4 started*
