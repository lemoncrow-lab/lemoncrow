---
gsd_state_version: 1.0
milestone: v0.3
milestone_name: Context Quality Execution
status: executing
last_updated: "2026-05-28T22:22:15.342Z"
last_activity: 2026-05-28 -- Phase 13 planning complete
progress:
  total_phases: 4
  completed_phases: 1
  total_plans: 5
  completed_plans: 1
  percent: 20
---

# Project State

**Project:** Atelier Public Benchmarks
**Milestone:** v0.3 — Context Quality Execution
**Updated:** 2026-05-28
**Status:** Ready to execute

## Current Phase

**Upcoming: Phase 13 — Phase-Linear Cache-Reuse Agent**

Next action: `/gsd-discuss-phase 13`

Phase 12 is complete. Phase 13 should implement the Survey→Plan cache-warm run mode, minified read context, auto mode selection, and local linear-vs-per-agent benchmark proof.

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

### v0.3 (Ready)

| Phase | Status | Goal |
|-------|--------|------|
| Phase 12 | ✅ Complete | Cache-Aware Routing (CACHE-01–05, CQEVAL-03) |
| Phase 13 | ⏳ Not started | Phase-Linear Cache-Reuse Agent (LINEAR-01–05, TBEVAL-01) |
| Phase 14 | ⏳ Not started | Counterexample Loop (COUNTER-01–05, CQEVAL-04) |
| Phase 15 | ⏳ Not started | Scoped Pull Context + Proof Gate (SCOPED-01–06, CQEVAL-05, TBEVAL-02) |

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

## Watch Points

- **`benchmarks/benchmarking.py`** — hardcoded fiction constants; DO NOT USE for published numbers
- **`ATELIER_DEV_MODE`** — ensure it cannot re-enable features in off-arm
- **Docusaurus `blog: false`** in `docs-site/docusaurus.config.ts` — must fix in Phase 5
- **Module-level singletons** in `mcp_server.py` (`_current_ledger`, `_realtime_ctx`) — subprocess isolation required
- **Uncommitted implementation changes** — source files already have modifications; inspect before editing and avoid overwriting user work
- **TerminalBench target** — v0.3 final proof should target ≥90% pass rate while cheaper and faster; if local proof fails, loop back into implementation

## Open Questions

- [ ] Which 10 specific TerminalBench task IDs to pin in `tasks.yaml` (review tbench.ai/tasks for <30min tasks)
- [ ] Non-Claude judge model choice: GPT-4o vs Gemini 1.5 Pro (Phase 7)

## Planning Artifacts

| File | Purpose |
|------|---------|
| `.planning/PROJECT.md` | Requirements, decisions, constraints |
| `.planning/REQUIREMENTS.md` | 47 v1 requirements with REQ-IDs |
| `.planning/ROADMAP.md` | 15-phase cumulative execution plan; v0.3 phases 12–15 active |
| `.planning/config.json` | YOLO mode, quality model, all agents on |
| `.planning/research/SUMMARY.md` | Synthesized research findings |
| `.planning/research/STACK.md` | Stack research (TerminalBench, claude -p schema) |
| `.planning/research/FEATURES.md` | Feature research (PR-replay, long-session) |
| `.planning/research/ARCHITECTURE.md` | Architecture patterns, resumability |
| `.planning/research/PITFALLS.md` | 18 pitfalls with severity tiers |
| `.planning/codebase/` | 7 codebase analysis documents |

## Current Position

Phase: 13 (Phase-Linear Cache-Reuse Agent) — READY TO DISCUSS
Previous phase: 12 (Cache-Aware Routing) — COMPLETE
Status: Ready to execute
Last activity: 2026-05-28 -- Phase 13 planning complete
