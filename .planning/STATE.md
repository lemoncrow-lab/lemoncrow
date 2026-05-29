---
gsd_state_version: 1.0
milestone: v0.4
milestone_name: Dedicated Language Support
status: executing
last_updated: "2026-05-29T13:10:00.000Z"
last_activity: 2026-05-29 -- Phase 18 planned; execution next
progress:
  total_phases: 6
  completed_phases: 2
  total_plans: 6
  completed_plans: 4
  percent: 33
---

# Project State

**Project:** Atelier Public Benchmarks
**Milestone:** v0.4 — Dedicated Language Support
**Updated:** 2026-05-29
**Status:** Executing Phase 18

## Current Phase

**Current: Phase 18 — Tree-sitter Repo-map Tags**

Next action: `/gsd-execute-phase 18`

Milestone v0.4 is defined from `docs/plans/dedicated-language-support/`. Phase 18 planning is complete with two execution plans for tree-sitter tag extraction and repo-map validation.

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
| Phase 18 | 🔄 Planned | Tree-sitter Repo-map Tags (DLS-TAGS-01–04) |
| Phase 19 | ⏳ Not started | Expanded SCIP Registry and Lazy Indexing (DLS-SCIP-01–04) |
| Phase 20 | ⏳ Not started | Runtime SCIP Indexer Provisioning (DLS-PROV-01–05) |
| Phase 21 | ⏳ Not started | Validation, Benchmarks, and Docs (DLS-VAL-01–04) |

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
| `.planning/research/SUMMARY.md` | Synthesized research findings |
| `.planning/research/STACK.md` | Stack research (TerminalBench, claude -p schema) |
| `.planning/research/FEATURES.md` | Feature research (PR-replay, long-session) |
| `.planning/research/ARCHITECTURE.md` | Architecture patterns, resumability |
| `.planning/research/PITFALLS.md` | 18 pitfalls with severity tiers |
| `.planning/codebase/` | 7 codebase analysis documents |

## Current Position

Phase: 18 (Tree-sitter Repo-map Tags) — READY TO PLAN
Previous phase: 17 (Tree-sitter Outline Coverage) — COMPLETE
Plans: TBD
Status: Planning Phase 18
Last activity: 2026-05-29 -- Phase 17 verified complete; Phase 18 planning next
