---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: verifying
stopped_at: Awaiting Phase 04 human approval
last_updated: "2026-05-19T13:35:00Z"
last_activity: 2026-05-19 -- Phase 04 automated verification cleared; human/UAT approval is now the only remaining gate
progress:
  total_phases: 7
  completed_phases: 3
  total_plans: 14
  completed_plans: 14
  percent: 43
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-05-19)

**Core value:** Agents can find and change code through budget-aware, precomputed intelligence with near-zero token overhead by default.
**Current focus:** Phase 04 — historical-code-intelligence

## Current Position

Phase: 04 (historical-code-intelligence) — HUMAN REVIEW PENDING
Plan: 04-01 through 04-04 complete; awaiting Phase 04 human/UAT approval
Status: Phase 04 automated verification is complete; only manual/UAT gates remain
Last activity: 2026-05-19 -- Phase 04 automated verification cleared; human/UAT approval is now the only remaining gate

Progress: [██████████] 100%

## Performance Metrics

**Velocity:**

- Total plans completed: 14
- Average duration: -
- Total execution time: 0.0 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 01 | 3 | - | - |
| 02 | 4 | - | - |

**Recent Trend:**

- Last 5 plans: -
- Trend: Stable

*Updated after each plan completion*
| Phase 01 P01 | 24min | 3 tasks | 10 files |
| Phase 01 P02 | 33m | 3 tasks | 11 files |
| Phase 01 P03 | 76m | 3 tasks | 9 files |
| Phase 02 P01 | 55min | 3 tasks | 12 files |
| Phase 02 P02 | 22min | 2 tasks | 11 files |
| Phase 02 P03 | inline | 3 tasks | 9 files |
| Phase 02 P04 | inline | 3 tasks | 12 files |
| Phase 03 P01 | 28min | 3 tasks | 10 files |
| Phase 03 P02 | 16min | 3 tasks | 8 files |
| Phase 03 P03 | 11min | 3 tasks | 15 files |
| Phase 04 P01 | 5min | 2 tasks | 10 files |
| Phase 04 P02 | 10min | 3 tasks | 9 files |
| Phase 04 P03 | 2 min | 2 tasks | 6 files |
| Phase 04 P04 | inline | 3 tasks | 10 files |

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- [Init] Use `docs/plans/active/code-intel/` M0-M18 as the project source of truth for delivery order and scope.
- [Init] Extend existing MCP/runtime surfaces before adding any new top-level tool registrations.
- [Init] Keep the M18 build-vs-integrate checkpoint as the gate for M16 large-repo backend work.
- [Phase 01]: Budget-fit helpers now size packed code payloads against the final wrapper envelope.
- [Phase 01]: Phase 1 benchmark coverage starts with deterministic repeated symbol-search smoke checks before threshold assertions.
- [Phase 01]: Use repo-local .atelier/cache/scip/<repo_id>/*.scip artifacts with local-only binary discovery for the Phase 1 M1 bootstrap path.
- [Phase 01]: Persist SCIP artifact signatures in engine_state so fresh CodeContextEngine instances invalidate stale retrieval-cache entries after artifact refresh.
- [Phase 01]: Default code search to snippet=none so hardened symbol lookup stays budget-safe unless callers opt into snippets.
- [Phase 01]: Measure the M2 token gate against serialized text-search-plus-read payloads versus low-budget single-hit code search.
- [Phase 02]: Resolve ast-grep availability through env override, exact binary discovery, then pinned managed bootstrap before returning `tool_unavailable`.
- [Phase 02]: Treat M12 as a partial close only until Plans 02-03 and 02-04 complete symbol-edit and usages follow-through validation.
- [Phase 02]: Use the file's exact line-span text rather than the dedented symbol payload when applying symbol replacements through rich edit.
- [Phase 02]: Missing routed usage data falls back explicitly to treesitter references instead of grep/text-search behavior.
- [Phase 03]: Keep semantic and hybrid ranking on the existing `code op="search"` surface with an additive `mode` parameter.
- [Phase 03]: Use a dedicated `embedding.py` helper with local cached embeddings so brownfield hotspots stay thin.
- [Phase 03]: Benchmark semantic quality through the existing benchmark landing zone and record M6 trace evidence against the milestone doc.
- [Phase 03]: Land M7 on memory op=recall_symbol and keep mcp_server.py to dependency wiring plus immediate delegation.
- [Phase 03]: Treat definition plus typed memory items as the default low-token recall bundle, with traces, decisions, and tests added only by explicit include.
- [Phase 03]: Extend the routed provider contract with typed caller/callee neighbors instead of adding a live fallback path.
- [Phase 03]: Keep traversal, cycle handling, and snapshot shaping in call_graph.py so engine.py and mcp_server.py stay thin.
- [Phase 03]: Ship snapshot as opt-in metadata while keeping depth=1 and snapshot=False as the cheap default.
- [Phase 04]: Pin pygit2 exactly at 1.19.2 and gate git-history code behind require_pygit2(). — Keeps bootstrap explicit and forbids hidden GitPython or subprocess fallback.
- [Phase 04]: Parse deleted and renamed blobs through extract_tags_from_text() rather than live file reads. — Deleted-history ingestion must work for blobs that no longer exist in the working tree.
- [Phase 04]: Keep deleted-history search on the existing `code op="search"` surface with additive `since` and `touched_by` params.
- [Phase 04]: Use a dedicated git-history adapter so `engine.py` stays on filter parsing, cache keys, and dispatch orchestration.
- [Phase 04]: Close M14 with the shipped deleted-search path benchmark plus explicit trace ownership.
- [Phase 04]: Keep stale-index handling infra-local in Wave 3 by returning freshness metadata instead of wiring public errors early. — Leaves public error shaping for Wave 4.
- [Phase 04]: Require trusted SCIP artifacts to carry a 40-character index_sha and preserve it in routed symbol payloads. — Lets later blame orchestration compare indexed data to HEAD without guessing.
- [Phase 04]: Treat routed SCIP `index_sha` metadata as the authoritative stale-index gate for `code op="blame"`, while local-only symbols fall back to current HEAD.
- [Phase 04]: Reuse shipped historical public surfaces in cost discipline by folding one deleted-history scenario and one blame scenario into the aggregate benchmark.

### Pending Todos

None yet.

### Blockers/Concerns

- Brownfield repository: existing worktree edits already touch `code_context` and MCP files, so execution plans must avoid overwriting unrelated changes.
- Phase 5 must complete the checkpoint plan before any M16 implementation work starts.
- Phase 1 plans were accepted with warning-level checker findings around plan breadth and pattern-map alignment; re-surface them during execution if file scope expands further.
- Phase 2 plans were accepted with warning-level checker findings about file breadth across `mcp_server.py`, `engine.py`, and related brownfield hotspots; re-surface them during execution if scope expands further.
- Broad repo tests still have earlier-phase failures outside Phase 04 scope; see `.planning/phases/04-historical-code-intelligence/deferred-items.md`.

## Deferred Items

Items acknowledged and carried forward from previous milestone close:

| Category | Item | Status | Deferred At |
|----------|------|--------|-------------|
| Validation | Broad repo tests still have unrelated earlier-phase red cases despite Phase 04 targeted suites passing | Deferred | 2026-05-19 |

## Session Continuity

Last session: 2026-05-19T13:35:00Z
Stopped at: Awaiting Phase 04 human approval
Resume file: None
