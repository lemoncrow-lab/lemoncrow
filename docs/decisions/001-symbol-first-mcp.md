# 001 Code intelligence: SCIP + ast-grep, not Serena

## Status

Proposed (2026-05-18)

## Context

Atelier's primary cost driver on coding tasks is LLM token spend on **finding
code** and **making targeted changes**. The two are intertwined: agents that
can't quickly locate a symbol end up reading whole files; agents that can't
edit at the symbol boundary end up regenerating large spans.

A first pass at this plan reached for Serena (`oraios/serena`) as the obvious
fit. On closer look Serena is a *live-LSP wrapper for MCP* — a thin protocol
shim around per-session language servers. That's the wrong abstraction for
cost optimisation:

- LSP servers cost a subprocess and a warm-up per session.
- Live LSP is single-workspace and single-language-server per file.
- It has no persistent index — every cold start re-pays the discovery cost.
- It has no notion of memory, of past edits, of decision history.
- Tool name mangling and connection drops are documented open issues.

The rest of the industry (GitHub, Sourcegraph, Meta, Google) solved this same
problem by **precomputing code intelligence into queryable artifacts** rather
than running language servers per session. The artifact format that won is
SCIP (Sourcegraph's open successor to LSIF). For *pattern-shaped* queries —
"find code that looks like X", which LSP and SCIP both handle poorly — the
industry standard is **ast-grep** (tree-sitter–native structural patterns,
single binary, cross-language).

## Decision

Atelier's code intelligence layer is built on:

1. **SCIP** as the primary symbol-intel artifact (`mcp__atelier__symbol`,
   `usages`, `callers/callees`). Precomputed, microsecond queries,
   deterministic, language-portable.
2. **ast-grep** as the primary structural-pattern primitive
   (`mcp__atelier__pattern`). Search and rewrite.
3. **A `SymbolIntelStore` composite** with content-addressed retrieval cache
   and token-budget enforcement. Routes by query shape; caches everything;
   packs the smallest sufficient payload. Mirrors the `OpenMemoryMemoryStore`
   pattern from `infra/memory_bridges/`.
4. **Function-level embeddings** layered over SCIP symbols for natural-
   language queries ("find auth functions") — something SCIP and LSP both
   cannot do.
5. **Atelier-only fusions** that no external tool can match: symbol↔memory
   recall, persistent bootstrap blocks, external-dep indexing, multi-repo
   workspaces.
6. **`LocalAdapter` (CodeContextEngine + LSP fallback)** as the always-on
   safety net for languages SCIP doesn't cover.

We do **not** adopt Serena. We do **not** rely on live LSP in the hot path.

## Consequences

- **No new top-level MCP tools.** Every milestone extends an already-
  registered tool in `src/atelier/gateway/adapters/mcp_server.py` (`code`,
  `edit`, `read`, `search`, `memory`) with a new `op` or descriptor kind. See
  [`../plans/active/code-intel/grounding.md`](../plans/active/code-intel/grounding.md)
  for the landing map.
- Agents stop reaching for `search` (text/regex) when they already know the
  name; the hardened `code op="search"` (M2) is the new default.
- One-time SCIP indexer install per language (auto-fetched static binaries).
- One-time ast-grep binary install (single static binary).
- Disk: a few hundred KB to tens of MB per repo for `.scip` files.
- Hot-path queries are subprocess-free after warm; latency in the
  microseconds.
- Cache hits return zero-token, zero-subprocess.
- A separate plan (`docs/plans/active/code-intel/`) tracks the 13 milestones.
- The full plan is gated by a cost-discipline benchmark
  (`tests/benchmarks/code_intel/bench_cost_discipline.py`): aggregate token
  cost across a 50-task suite must drop to ≤ 30% of the pre-implementation
  baseline.

## Enforcement

- New tools land with tests under `tests/` and rows in
  `docs/agent-os/validation-matrix.md`.
- New taste invariants in `docs/agent-os/taste-invariants.md`:
  - *"If the caller already knows the symbol name, do not run a text search."*
  - *"Default to outline-first responses. Expand only on intent."*
  - *"Never edit at line numbers when the target is a named symbol."*
- New scorecard metrics in `docs/quality/scorecard.md`:
  - % of code-intel tool calls hitting cache (target ≥ 40%).
  - % of navigation tasks using `code op="search"` vs `tool_smart_search` (target ≥ 70%).
  - Median tokens per navigation task (target ≤ 25% of baseline).
  - Median tokens per refactor task (target ≤ 30% of baseline).
- Plan: [`docs/plans/active/code-intel/index.md`](../plans/active/code-intel/index.md).
