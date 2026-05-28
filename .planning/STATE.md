# Project State

**Project:** Atelier Public Benchmarks
**Milestone:** v0.1
**Updated:** 2026-05-28
**Status:** Ready to execute Phase 1

## Current Phase

**Next up: Phase 1 — Bench-Mode Toggle** *(in progress)*

No phases have been executed yet. Initialization complete.

## Roadmap Progress

| Phase | Status | Goal |
|-------|--------|------|
| Phase 1 | 🔲 Not started | Bench-mode toggle (MODE-01–08) |
| Phase 2 | 🔲 Not started | TerminalBench adapter (TB-01–05) |
| Phase 3 | 🔲 Not started | A/B runner (AB-01–06) |
| Phase 4 | 🔲 Not started | Report generator (RPT-01–06) |
| Phase 5 | 🔲 Not started | Publication pipeline (PUB-01–05) |
| Phase 6 | 🔲 Not started | Long-session suite + CLI (LS-01–04, CLI-01–06) |
| Phase 7 | 🔲 Not started | PR-replay benchmarks (PR-01–06) |

## Key Decisions Log

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Python version for benchmarks | Isolated 3.12 workspace (`benchmarks/pyproject.toml`) | TerminalBench requires ≥3.12; main project on 3.11 |
| Token counting | API `usage` field only | tiktoken cl100k_base has 10-30% error on Claude |
| CI method | Wilson score (inline math) | Normal approx invalid at N=5; scipy not available |
| Arm interleaving | Rep-by-rep (not batched) | Equalizes prompt cache temperature between arms |
| Judge model for PR-replay | Non-Claude (GPT-4o or Gemini) | Avoid self-judging bias |
| State leakage prevention | Separate `ATELIER_ROOT` per arm | Shared filesystem state contaminates off-arm |

## Watch Points

- **`benchmarks/benchmarking.py`** — hardcoded fiction constants; DO NOT USE for published numbers
- **`ATELIER_DEV_MODE`** — ensure it cannot re-enable features in off-arm
- **Docusaurus `blog: false`** in `docs-site/docusaurus.config.ts` — must fix in Phase 5
- **Module-level singletons** in `mcp_server.py` (`_current_ledger`, `_realtime_ctx`) — subprocess isolation required

## Open Questions

- [ ] Which 10 specific TerminalBench task IDs to pin in `tasks.yaml` (review tbench.ai/tasks for <30min tasks)
- [ ] Non-Claude judge model choice: GPT-4o vs Gemini 1.5 Pro (Phase 7)

## Planning Artifacts

| File | Purpose |
|------|---------|
| `.planning/PROJECT.md` | Requirements, decisions, constraints |
| `.planning/REQUIREMENTS.md` | 47 v1 requirements with REQ-IDs |
| `.planning/ROADMAP.md` | 7-phase execution plan |
| `.planning/config.json` | YOLO mode, quality model, all agents on |
| `.planning/research/SUMMARY.md` | Synthesized research findings |
| `.planning/research/STACK.md` | Stack research (TerminalBench, claude -p schema) |
| `.planning/research/FEATURES.md` | Feature research (PR-replay, long-session) |
| `.planning/research/ARCHITECTURE.md` | Architecture patterns, resumability |
| `.planning/research/PITFALLS.md` | 18 pitfalls with severity tiers |
| `.planning/codebase/` | 7 codebase analysis documents |
