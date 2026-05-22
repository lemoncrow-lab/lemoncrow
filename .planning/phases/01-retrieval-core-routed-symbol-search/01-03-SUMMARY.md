---
phase: 01-retrieval-core-routed-symbol-search
plan: "03"
subsystem: code-context
tags: [code-intel, code-search, snippets, benchmarks, validation]
requires:
  - phase: 01-02
    provides: routed SCIP-backed symbol provenance on the existing `code` surface
provides:
  - Hardened `code op="search"` params for snippet, scope, and file-glob routing
  - Budget-safe snippet-free default search responses with provenance breakdown
  - M2 benchmark and trace evidence for Phase 1 closeout
affects: [Phase 2 search/edit work, validation guidance, code-context]
tech-stack:
  added: []
  patterns:
    - Snippet-free default symbol search with additive opt-in snippet modes
    - Budget fitting that can drop trailing search hits when the minimal payload would overflow
    - Validation closure through benchmark gates plus recorded milestone traces
key-files:
  created: []
  modified:
    - src/atelier/core/capabilities/code_context/engine.py
    - src/atelier/core/capabilities/code_context/models.py
    - src/atelier/gateway/adapters/mcp_server.py
    - tests/core/test_code_context.py
    - tests/gateway/test_p0_mcp_surfaces.py
    - tests/gateway/test_mcp_tool_handlers.py
    - tests/benchmarks/code_intel/test_symbol_search_bench.py
    - docs/agent-os/validation-matrix.md
    - .planning/phases/01-retrieval-core-routed-symbol-search/01-VALIDATION.md
key-decisions:
  - "Default `code op=\"search\"` to `snippet=\"none\"` so the low-token path stays budget-safe unless callers opt into snippets."
  - "Enforce the M2 token ceiling with a deterministic serialized text-search-plus-read baseline versus low-budget single-hit code search."
patterns-established:
  - "Gateway and engine search defaults omit `snippet` keys entirely when snippets are disabled."
  - "Phase closeout requires both milestone trace records and benchmark thresholds before validation flips to approved."
requirements-completed: [NAVG-01]
duration: 1h 16m
completed: 2026-05-18
---

# Phase 1 Plan 3: Retrieval Core & Routed Symbol Search Summary

**Hardened `code op="search"` with additive snippet/scope filters, budget-safe default payloads, and trace-backed M2 validation evidence**

## Performance

- **Duration:** 1h 16m
- **Started:** 2026-05-18T19:57:00Z
- **Completed:** 2026-05-18T21:13:01Z
- **Tasks:** 3
- **Files modified:** 9

## Accomplishments
- Added hardened `code` search parameters on the existing MCP tool without adding a new top-level surface.
- Kept ranked symbol search within budget by defaulting to snippet-free payloads, omitting empty snippet fields, and dropping trailing hits when needed.
- Closed Phase 1 validation with the M2 token benchmark, updated validation guidance, and recorded M0/M1/M2 traces on the existing trace surface.

## Verification

- `uv run pytest tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_tool_handlers.py -q`
- `uv run pytest tests/core/test_code_context.py tests/gateway/test_savings_api.py -q`
- `uv run pytest tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_tool_handlers.py tests/benchmarks/code_intel/test_symbol_search_bench.py::test_symbol_search_uses_at_most_25pct_of_text_search_tokens -q`
- `uv run pytest tests/core/test_code_context.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_savings_api.py tests/benchmarks/code_intel/test_symbol_search_bench.py -q`
- `make lint`
- `make typecheck`
- `make test` *(still fails in pre-existing unrelated infra suites already tracked in `deferred-items.md`)*

## Task Commits

1. **Task 1: Extend the existing `code op="search"` contract with additive hardened parameters**
   - `b67d649` (`test`) add failing hardened code search gateway coverage
   - `eb2e854` (`feat`) harden code search surface
2. **Task 2: Harden ranking, snippet packing, and telemetry on the routed search path**
   - `9ac345e` (`test`) add failing snippet packing regressions
   - `44f2cd7` (`feat`) tighten snippet packing and ranking defaults
3. **Task 3: Enforce M2 cost targets, record M0/M1/M2 traces, and close Phase 1 validation**
   - `852d1e7` (`test`) close validation and benchmark evidence

## Files Created/Modified
- `src/atelier/core/capabilities/code_context/engine.py` - added hardened search params, snippet attachment, provenance breakdown, and budget overflow trimming
- `src/atelier/core/capabilities/code_context/models.py` - extended `SymbolRecord` with optional snippet support
- `src/atelier/gateway/adapters/mcp_server.py` - exposed additive search params and snippet-free defaults on the existing `code` op
- `tests/core/test_code_context.py` - locked snippet-free defaults, exact-match ordering, and budget trimming regressions
- `tests/gateway/test_p0_mcp_surfaces.py` - asserted default `code` search responses stay snippet-free at the public MCP boundary
- `tests/gateway/test_mcp_tool_handlers.py` - verified hardened search fields and unchanged MCP tool behavior end to end
- `tests/benchmarks/code_intel/test_symbol_search_bench.py` - added the M2 ≤25%-of-baseline token benchmark
- `docs/agent-os/validation-matrix.md` - required benchmark gates alongside the targeted code-intel pytest suite
- `.planning/phases/01-retrieval-core-routed-symbol-search/01-VALIDATION.md` - closed Wave 0, recorded trace requirements, and marked validation approved

## Decisions Made
- Defaulted hardened search to `snippet="none"` so the cheapest navigation path stays within budget unless the caller explicitly requests snippets.
- Measured the M2 token gate against serialized text-search-plus-read payloads and a low-budget single-hit code-search path to reflect actual response-envelope cost.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed budget overflow introduced by snippet-aware search defaults**
- **Found during:** Task 2 (Harden ranking, snippet packing, and telemetry on the routed search path)
- **Issue:** default snippet-bearing search payloads pushed `tool_search` over `budget_tokens`, breaking an existing regression and the plan’s cost target.
- **Fix:** defaulted search to `snippet="none"`, omitted empty snippet keys from serialized items, and added fallback trimming that drops trailing hits when the minimal payload still overflows.
- **Files modified:** `src/atelier/core/capabilities/code_context/engine.py`, `src/atelier/gateway/adapters/mcp_server.py`, `tests/core/test_code_context.py`
- **Verification:** `uv run pytest tests/core/test_code_context.py tests/gateway/test_savings_api.py -q`
- **Committed in:** `44f2cd7`

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** The fix stayed within the planned search-hardening scope and was required for correctness, budget compliance, and the M2 benchmark gate.

## Issues Encountered
- `make test` still fails in unrelated pre-existing infra suites already documented in `.planning/phases/01-retrieval-core-routed-symbol-search/deferred-items.md`; targeted Phase 1 regressions, the M2 benchmark gate, lint, and typecheck are green.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Phase 1 is closed with routed provenance, hardened symbol search defaults, and explicit validation guidance for future code-intel work.
- Phase 2 can build on the existing `code` tool surface without reopening Phase 1 budget/trace/benchmark questions.

## Known Stubs
None.

## Self-Check: PASSED
- FOUND: `.planning/phases/01-retrieval-core-routed-symbol-search/01-03-SUMMARY.md`
- FOUND commits: `b67d649`, `eb2e854`, `9ac345e`, `44f2cd7`, `852d1e7`
