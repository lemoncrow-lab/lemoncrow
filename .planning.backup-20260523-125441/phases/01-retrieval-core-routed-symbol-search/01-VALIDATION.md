---
phase: 1
slug: retrieval-core-routed-symbol-search
status: approved
nyquist_compliant: true
wave_0_complete: true
created: 2026-05-18
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + existing repo make targets |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `uv run pytest tests/core/test_code_context.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_savings_api.py tests/benchmarks/code_intel/test_symbol_search_bench.py -q` |
| **Full suite command** | `make lint && make typecheck && make test` |
| **Estimated runtime** | ~30-300 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/core/test_code_context.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_savings_api.py -q`
- **After every plan wave:** Run `make lint && make typecheck && make test`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 300 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 01-01-01 | 01-01 | 1 | FNDN-01 | — | Cache, provenance, and token metadata stay on the existing `code` response shape | unit + gateway | `uv run pytest tests/core/test_code_context.py tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_savings_api.py -q` | ✅ | ✅ green |
| 01-02-01 | 01-02 | 2 | FNDN-02 | — | Routed SCIP backend preserves fallback behavior and does not add new top-level MCP tools | unit + gateway | `uv run pytest tests/core/test_code_context.py tests/gateway/test_mcp_tool_handlers.py -q` | ✅ | ✅ green |
| 01-03-01 | 01-03 | 3 | NAVG-01 | T-01-03-01, T-01-03-02, T-01-03-03 | Hardened `code op="search"` adds snippet/ranking behavior without breaking existing MCP contracts | gateway + benchmark | `uv run pytest tests/gateway/test_p0_mcp_surfaces.py tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_savings_api.py tests/benchmarks/code_intel/test_symbol_search_bench.py::test_symbol_search_uses_at_most_25pct_of_text_search_tokens -q` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/benchmarks/code_intel/` — deterministic smoke, routed SCIP thresholds, and the M2 ≤25%-of-baseline benchmark now exist under `tests/benchmarks/code_intel/test_symbol_search_bench.py`
- [x] `tests/core/test_code_context.py` — fixtures now cover cache invalidation, snippet-free defaults, budget trimming, and routed provenance expectations
- [x] `tests/gateway/test_p0_mcp_surfaces.py` and `tests/gateway/test_mcp_tool_handlers.py` — coverage asserts hardened `tool_code` fields while preserving the existing MCP surface

## Trace Evidence

- Recorded Phase 1 milestone closeout traces on the existing `trace` surface for:
  - `docs/plans/active/code-intel/M0-store.md`
  - `docs/plans/active/code-intel/M1-scip-adapter.md`
  - `docs/plans/active/code-intel/M2-symbol-tool.md`
- Verification query:
  `uv run python -c "import os; from pathlib import Path; from atelier.core.foundation.paths import default_store_root; from atelier.core.foundation.store import ContextStore; root = Path(os.environ.get('ATELIER_ROOT') or str(default_store_root())); store = ContextStore(root); store.init(); milestones = ['M0-store.md', 'M1-scip-adapter.md', 'M2-symbol-tool.md']; matches = {milestone: [trace.id for trace in store.list_traces(query=milestone, limit=25)] for milestone in milestones}; assert all(matches[milestone] for milestone in milestones), matches; assert len({trace_id for trace_ids in matches.values() for trace_id in trace_ids}) >= 3, matches"`

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Validate brownfield-safe coexistence with the user's in-flight `code_context` and MCP edits | FNDN-01, FNDN-02, NAVG-01 | Existing worktree changes increase merge/overwrite risk that automated tests do not fully express | Review the diff for `src/atelier/core/capabilities/code_context/` and `src/atelier/gateway/adapters/mcp_server.py` before phase completion; confirm planned tasks only narrow/complete current edits rather than replacing them wholesale. |
| Confirm SCIP bootstrap assumptions match available local toolchains | FNDN-02 | Local environment lacks `go`, so initial Phase 1 indexer support must stay within realistic Python/TypeScript paths | Validate the planned indexer/bootstrap steps against actual available binaries before execution; if a toolchain is unavailable, the plan must document the fallback or narrow the scope. |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 300s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved after benchmark and trace evidence capture
