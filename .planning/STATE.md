---
gsd_state_version: 1.0
milestone: v0.5
milestone_name: Quality & Benchmark Lift
status: executing
last_updated: "2026-05-29T18:05:41.778Z"
last_activity: 2026-05-29 -- Completed 23-02-PLAN.md (9 SDK/core/infra silent exceptions observable; 4 BLE001 ignores removed)
progress:
  total_phases: 6
  completed_phases: 1
  total_plans: 4
  completed_plans: 3
  percent: 17
---

# Project State

**Project:** Atelier Public Benchmarks
**Milestone:** v0.5 — Quality & Benchmark Lift
**Updated:** 2026-05-29
**Status:** Ready to execute

## Current Phase

**Current: Phase 22 — Lint and Coverage Gates**

Next action: `/gsd-plan-phase 22`

Milestone v0.5 is defined from `docs/plans/quality-and-benchmark-lift/`. It focuses on coding-quality gates, silent exception burn-down, stdout/logging hygiene, CLI decomposition, expanded A/B suites, and reproducible public benchmark results.

## Roadmap Progress

### v0.1 (Complete)

| Phase | Status | Goal |
|-------|--------|------|
| Phase 1 | ✅ Complete | Bench-mode toggle (MODE-01–08) |
| Phase 2 | ✅ Complete | TerminalBench adapter (TB-01–05) |
| Phase 3 | ✅ Complete | A/B runner (AB-01–06) |
| Phase 4 | ✅ Complete | Report generator (RPT-01–06) |
| Phase 5 | ✅ Complete | Publication pipeline (PUB-01–05) |
| Phase 6 | ✅ Complete | Long-session suite + CLI (LS-01–04, CLI-01–06) |
| Phase 7 | ✅ Complete | PR-replay benchmarks (PR-01–06) |

### v0.2 (Context Lineage Complete; Remaining Scope Superseded)

| Phase | Status | Goal |
|-------|--------|------|
| Phase 8 | ✅ Complete | Context Lineage (LINEAGE-01–06, CQEVAL-01–02) |
| Phase 9 | ↪ Superseded | Cache-Aware Routing moved to Phase 12 |
| Phase 10 | ↪ Superseded | Counterexample Loop moved to Phase 14 |
| Phase 11 | ↪ Superseded | Scoped Pull Context moved to Phase 15 |

### v0.3 (Context Quality Execution)

| Phase | Status | Goal |
|-------|--------|------|
| Phase 12 | ✅ Complete | Cache-Aware Routing (CACHE-01–05, CQEVAL-03) |
| Phase 13 | ✅ Complete | Phase-Linear Cache-Reuse Agent (LINEAR-01–05, TBEVAL-01) |
| Phase 14 | ⏳ Not started | Counterexample Loop (COUNTER-01–05, CQEVAL-04) |
| Phase 15 | ⏳ Not started | Scoped Pull Context + Proof Gate (SCOPED-01–06, CQEVAL-05, TBEVAL-02) |

### v0.4 (Ready)

| Phase | Status | Goal |
|-------|--------|------|
| Phase 16 | ✅ Complete | Canonical Language Registry (DLS-LANG-01–04) |
| Phase 17 | ✅ Complete | Tree-sitter Outline Coverage (DLS-OUTLINE-01–05) |
| Phase 18 | ✅ Complete | Tree-sitter Repo-map Tags (DLS-TAGS-01–04) |
| Phase 19 | ✅ Complete | Expanded SCIP Registry and Lazy Indexing (DLS-SCIP-01–04) |
| Phase 20 | ✅ Complete | Runtime SCIP Indexer Provisioning (DLS-PROV-01–05) |
| Phase 21 | ✅ Complete | Validation, Benchmarks, and Docs (DLS-VAL-01–04) |

### v0.5 (Active)

| Phase | Status | Goal |
|-------|--------|------|
| Phase 22 | ⏳ Not started | Lint and Coverage Gates (QBL-GATE-01–05) |
| Phase 23 | ⏳ Not started | Silent Exception Audit (QBL-EXC-01–04) |
| Phase 24 | ⏳ Not started | Stdout to Logging (QBL-LOG-01–04) |
| Phase 25 | ⏳ Not started | CLI Decomposition (QBL-CLI-01–04) |
| Phase 26 | ⏳ Not started | A/B Suite Expansion (QBL-AB-01–04) |
| Phase 27 | ⏳ Not started | Public Benchmark Results (QBL-RES-01–04) |

### v0.6 (World-Class Atelier - Ready)

| Phase | Status | Goal |
|-------|--------|------|
| Phase 28 | ⏳ Not started | Neural Code Embeddings (WCA-EMB-01–06) |
| Phase 29 | ⏳ Not started | Empirical Proof Program (WCA-PROOF-01–04) |
| Phase 30 | ⏳ Not started | Cross-encoder Reranker (WCA-RERANK-01–03) |
| Phase 31 | ⏳ Not started | Phase-aware Workflow (WCA-STEM-01–04) |
| Phase 32 | ⏳ Not started | Calibrated & Enforcing Routing (WCA-ROUTE-01–02) |
| Phase 33 | ⏳ Not started | Continuous Indexing (WCA-INDEX-01–03) |
| Phase 34 | ⏳ Not started | Speculative Retrieval (WCA-SPEC-01–03) |

## Key Decisions Log

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Python version for benchmarks | Isolated 3.12 workspace (`benchmarks/pyproject.toml`) | TerminalBench requires ≥3.12; main project on 3.11 |
| Token counting | API `usage` field only | tiktoken cl100k_base has 10-30% error on Claude |
| CI method | Wilson score (inline math) | Normal approx invalid at N=5; scipy not available |
| parse_stream_jsonl return keys | Mapped names (cost_usd, latency_ms) not raw JSON names | CRITICAL spec in prompt; run_terminalbench_trial uses same names |
| AtelierClaudeAgent._env | Minimal dict only — NOT full os.environ | T-02-04 threat: prevents host dev contamination (ATELIER_DEV_MODE excluded) |
| Judge model for PR-replay | Non-Claude (GPT-4o or Gemini) | Avoid self-judging bias |
| State leakage prevention | Separate `ATELIER_ROOT` per arm | Shared filesystem state contaminates off-arm |
| Phase-linear cache reuse | Survey→Plan continuity + minified reads | Reduce cost/latency without model downgrade |
| v0.3 autonomy | Execute end-to-end without approval gates unless blocked by conflicting user changes | User requested autonomous execution and local proof benchmarks |
| Canonical language names | Use tree-sitter parser names as the shared key set across code-intel surfaces | Fixes shell/bash drift and prevents future spelling mismatches |
| SCIP provisioning tiers | Install cheap Python/TypeScript indexers, lazy-fetch medium indexers, detect/document heavy toolchain indexers | Semantic intel should work out of the box where practical without forcing large toolchains |
| Quality lift starts with gates before burn-down | Enable BLE001/T20 with per-file ignores first so new debt fails while existing debt is fixed phase-by-phase | Prevents backsliding while keeping the milestone reviewable |
| Public benchmark claims must be regression-gated | README savings claims need runnable A/B suites and CI thresholds before they are credible | Makes public claims reproducible and auditable |

## Watch Points

- **`benchmarks/benchmarking.py`** — hardcoded fiction constants; DO NOT USE for published numbers
- **`ATELIER_DEV_MODE`** — ensure it cannot re-enable features in off-arm
- **Docusaurus `blog: false`** in `docs-site/docusaurus.config.ts` — must fix in Phase 5
- **Module-level singletons** in `mcp_server.py` (`_current_ledger`, `_realtime_ctx`) — subprocess isolation required
- **Uncommitted implementation changes** — source files already have modifications; inspect before editing and avoid overwriting user work
- **TerminalBench target** — v0.3 final proof should target ≥90% pass rate while cheaper and faster; if local proof fails, loop back into implementation
- **Language registry drift** — do not add new extension maps, parser-key maps, or SCIP language maps outside the canonical registry
- **SCIP binary installation size** — keep heavy toolchains opt-in/detected; avoid silently installing Rust/Java/C++ ecosystems

## Open Questions

- [ ] Which 10 specific TerminalBench task IDs to pin in `tasks.yaml` (review tbench.ai/tasks for <30min tasks)
- [ ] Non-Claude judge model choice: GPT-4o vs Gemini 1.5 Pro (Phase 7)
- [ ] Confirm exact tree-sitter-language-pack parser names for `yaml`, `toml`, `json`, `sql`, and C# canonical spelling during Phase 16/17 implementation
- [ ] Confirm checksum source and allowlist format for Tier-2 SCIP lazy downloads during Phase 20 implementation

## Planning Artifacts

| File | Purpose |
|------|---------|
| `.planning/PROJECT.md` | Requirements, decisions, constraints |
| `.planning/REQUIREMENTS.md` | Cumulative requirements; v0.4 DLS requirements active |
| `.planning/ROADMAP.md` | 21-phase cumulative execution plan; v0.4 phases 16–21 active |
| `.planning/config.json` | YOLO mode, quality model, all agents on |
| `docs/plans/dedicated-language-support/` | Source design plan for v0.4 |
| `docs/plans/quality-and-benchmark-lift/` | Source design plan for v0.5 |
| `.planning/research/SUMMARY.md` | Synthesized research findings |
| `.planning/research/STACK.md` | Stack research (TerminalBench, claude -p schema) |
| `.planning/research/FEATURES.md` | Feature research (PR-replay, long-session) |
| `.planning/research/ARCHITECTURE.md` | Architecture patterns, resumability |
| `.planning/research/PITFALLS.md` | 18 pitfalls with severity tiers |
| `.planning/codebase/` | 7 codebase analysis documents |

## Current Position

Phase: 23 (silent-exception-audit) — EXECUTING
Plan: 3 of 3
Previous milestone: v0.4 (Dedicated Language Support) — COMPLETE
Plans: TBD
Status: Executing Phase 23
Last activity: 2026-05-29 -- Completed 23-02-PLAN.md (9 SDK/core/infra silent exceptions observable; 4 BLE001 ignores removed; src commits 6f08d89, c86ca8d)
