# Atelier code intelligence — cost-optimal stack

> Status: **Active** — created 2026-05-18, revised 2026-05-18 (grounded).
> Owner: unassigned.
> ADR: [`../../decisions/001-symbol-first-mcp.md`](../../../decisions/001-symbol-first-mcp.md).
> Tasks: see `TaskList` (numbered M0–M18).

> ⚠️ **Read [`grounding.md`](grounding.md) before any milestone file.** This
> plan extends the existing MCP tools registered in
> `src/atelier/gateway/adapters/mcp_server.py` (`code`, `edit`, `read`,
> `search`, `memory`, …). We do **not** add new top-level `@mcp_tool` entries
> unless a milestone file explicitly says so. Most milestones add a new `op`
> to an existing tool and a new internal module under `core/capabilities/` or
> `infra/`. If a milestone file disagrees with `grounding.md`, the grounding
> doc wins.

## North star

**Reduce LLM cost on coding tasks by giving the agent an environment where
finding code and changing code are near-zero-token operations.**

Every milestone is justified by token savings, not feature parity with another
tool. We do not adopt Serena. We adopt the artifacts that the rest of the
industry (GitHub, Sourcegraph, Meta, Google) settled on for the same reason:
*precomputed code intelligence beats live LSP per session.*

## Stack at a glance

Each row below is an **op on an existing MCP tool**, not a new tool
registration. See `grounding.md` for the full landing map.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ MCP surface — existing tools, extended with new ops                      │
│                                                                          │
│   code(op="search", …)             ← name-first symbol lookup (exists)  │
│   code(op="usages", …)             ← M3, NEW op                         │
│   code(op="callers"|"callees", …)  ← M8, NEW ops                        │
│   code(op="pattern", …)            ← M5, NEW op (ast-grep)              │
│   code(op="recall", …)             ← M7, NEW op (or under memory)       │
│   code(op="blame", …)              ← M15, NEW op (who/when/churn)       │
│   code(op="search" scope="deleted" since="Nd" touched_by=X)             │
│                                    ← M14, temporal + graveyard filters   │
│   edit(edits=[{kind:"symbol",…}])  ← M4, NEW rich-edit descriptor       │
│   read(...)                        ← already outline-first; no M change  │
│   search(...)                      ← stays as text/regex complement      │
│   memory(op="recall_symbol", …)    ← M7 alternative; choose on claim    │
└────────────────────────────┬─────────────────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────────────────┐
│ SymbolIntelStore (routing + cache + budget + query-shape detection)      │
│  - name query   → SCIP (microseconds, precomputed)                       │
│  - pattern      → ast-grep (structural, cross-language)                  │
│  - NL query     → embeddings (semantic, hybrid RRF)                      │
│  - text search  → Zoekt (>500k LOC) or ripgrep (small repos)            │
│  - deleted/hist → Git History Index (pygit2)                             │
│  - fallback     → LocalAdapter (CodeContextEngine + tree-sitter)         │
│  Content-addressed cache · token-budget packer · trace always on         │
└──┬──────────┬───────────┬──────────┬────────────┬────────────────────────┘
   │          │           │          │            │
   ▼          ▼           ▼          ▼            ▼
┌──────┐ ┌────────┐ ┌──────────┐ ┌──────┐ ┌─────────────────────────────┐
│ SCIP │ │ast-grep│ │Embeddings│ │Zoekt │ │ Git History (M14, M15)      │
│  M1  │ │   M5   │ │   M6     │ │ M16  │ │  walker.py  (pygit2)        │
│★core │ │★core   │ │(vec+RRF) │ │scale │ │  graveyard.py  (SQLite)     │
└──────┘ └────────┘ └──────────┘ └──────┘ │  blame.py   (churn score)  │
   │                                       └────────────┬────────────────┘
   ▼                                                    │
.scip files                                ┌────────────▼────────────────┐
precomputed once,                          │ Symbol Graveyard DB         │
queried in µs                              │ deleted · renamed · blame   │
(scip-python ·                             │ churn · temporal filters    │
 scip-typescript ·                         └─────────────────────────────┘
 scip-go · scip-rust ·
 scip-java · …)

   Cross-language edges (M17, partial):
┌───────────────────────────────────────┐
│  ctypes/cffi  Python→C                │
│  subprocess   TypeScript/Go→Python    │
│  dynamic_import  Python→Python        │
│  confidence-scored · soft edges ok   │
└───────────────────────────────────────┘

   Optional backend (decided in M18 evaluation):
┌──────────────────────────────────────┐
│  SrcCliAdapter  (if org runs SG)     │
│  or Sourcegraph embedded backend     │
│  replaces Zoekt if memory-feasible   │
└──────────────────────────────────────┘
```

## Why this stack

| Choice | Why over the obvious alternative |
|---|---|
| **SCIP indexes** | 100–1000× faster than live LSP. No subprocess. Deterministic. The format GitHub and Sourcegraph use precisely because LSP-per-session doesn't scale. Cold queries are sub-millisecond. |
| **ast-grep** | Structural patterns (`$AUTH.verify($USER)`) instead of regex. Cross-language, tree-sitter native, single binary. Direct competitor to Serena's `replace_symbol_body` for refactors — and works without an LSP at all. |
| **Function-level embeddings** | "Find auth functions" is impossible with name search and unreliable with file-level embeddings. Chunk on tree-sitter symbol boundaries, hybrid lexical + vector rerank. |
| **Local SQLite cache** | Same query twice = zero LLM tokens, zero subprocess cost. Content-addressed so renames don't bust the cache. |
| **Token-budget enforcer** | Caller declares `budget_tokens=N`; store packs the most informative payload that fits. Outline first, expand on demand. This single feature is worth more than any individual index. |
| **No Serena** | LSP-per-session is the wrong abstraction. We adopt the *artifacts* (SCIP) the LSP ecosystem produces, not the live protocol. |
| **Git history index** (M14) | SCIP only indexes HEAD. Deleted and renamed symbols are invisible to every other tool. A pygit2-backed graveyard makes them first-class query targets with zero token overhead. |
| **Blame + churn score** (M15) | "Who last touched this, and how often does it change?" collapses from 4 tool calls + regex parsing to one. Churn score distinguishes stable APIs from hotspots before the agent touches them. |
| **Zoekt at scale** (M16) | Ripgrep reads files on every query — fast for small repos, breaks at >1M LOC. Zoekt (Google's own engine, maintained by Sourcegraph) precomputes a trigram index; queries stay ~5ms regardless of repo size. |
| **Cross-language edges** (M17) | ctypes/cffi, subprocess bridges, and dynamic imports create invisible reference gaps between language indexes. Confidence-scored edges surface the most common 60% of cross-language calls without requiring runtime analysis. |

## What we already have (reuse, don't rebuild)

| Atelier asset | Used by |
|---|---|
| `CodeContextEngine` (SQLite + FTS + AST) | LocalAdapter fallback, retrieval cache, embedding storage |
| `repo_map` PageRank | Bootstrap (M11), seed for embedding rerank (M6) |
| `semantic_file_memory` | Change-impact analysis, dependency graph |
| `infra/tree_sitter/tags.py` | LocalAdapter when SCIP indexer absent for a language |
| `infra/embeddings/` | Function-level vectorisation (M6) |
| `infra/memory_bridges/` pattern | Prior art only; M0 lands directly in `core/capabilities/code_context/` with no `infra/code_intel_bridges/` directory |
| Memory blocks + recall | Symbol↔memory fusion (M7) |
| Trace recording | Every store call writes a trace; feeds the scorecard |

## Milestones

Each milestone has its own file. Claim one via `TaskUpdate owner=...` before
opening the file to edit. **Read `index.md` and your milestone file before
starting; do not read milestones you are not assigned to.**

| ID | File | What it ships |
|----|------|---------------|
| M0 | [`M0-store.md`](M0-store.md) | Retrieval cache + budget packer inside `code_context`; refactor `code` ops to route through them |
| M1 | [`M1-scip-adapter.md`](M1-scip-adapter.md) | SCIP backend behind `CodeContextEngine` query methods; indexer + reader + watcher |
| M2 | [`M2-symbol-tool.md`](M2-symbol-tool.md) | Harden `code op="search"` defaults (snippet, ranking, provenance) |
| M3 | [`M3-usages-tool.md`](M3-usages-tool.md) | `code op="usages"` new op |
| M4 | [`M4-edit-symbol.md`](M4-edit-symbol.md) | `edit` new rich descriptor `kind="symbol"` |
| M5 | [`M5-astgrep-pattern.md`](M5-astgrep-pattern.md) | `code op="pattern"` new op (ast-grep adapter) |
| M6 | [`M6-semantic-rank.md`](M6-semantic-rank.md) | Function-level embeddings inside `CodeContextEngine.search_symbols` |
| M7 | [`M7-recall-symbol.md`](M7-recall-symbol.md) | `memory op="recall_symbol"` (or `code op="recall"`) — pick on claim |
| M8 | [`M8-call-graph.md`](M8-call-graph.md) | `code op="callers"` / `op="callees"` |
| M9 | [`M9-external-deps.md`](M9-external-deps.md) | `scope="external"` field on `code op="search"`; engine indexer flags |
| M10 | [`M10-multi-repo.md`](M10-multi-repo.md) | Multi-root in `CodeContextEngine`; `.atelier/workspace.toml` reader; `repo` field where useful |
| M11 | [`M11-bootstrap.md`](M11-bootstrap.md) | First-context job pipeline; pinned memory blocks |
| M12 | [`M12-token-budget.md`](M12-token-budget.md) | Audit defaults across `code`/`read`/`edit`/`search` for outline-first; sharpen M0 cache + budget |
| M13 | [`M13-docs.md`](M13-docs.md) | agent-os playbooks + scorecard metrics |
| M14 | [`M14-git-history.md`](M14-git-history.md) | Git history index (pygit2); deleted/renamed symbol graveyard; `scope="deleted"`, `since=`, `touched_by=` filters |
| M15 | [`M15-blame-temporal.md`](M15-blame-temporal.md) | `code op="blame"` — last author, age, churn score; temporal filters on live SCIP search |
| M16 | [`M16-zoekt-scale.md`](M16-zoekt-scale.md) | Zoekt backend for repos >500k LOC; auto-routing in SymbolIntelStore; blocked on M18 |
| M17 | [`M17-cross-lang.md`](M17-cross-lang.md) | Cross-language edges: ctypes/cffi (Python→C), subprocess (TS/Go→Python), dynamic imports; confidence-scored |
| M18 | [`M18-bvi-checkpoint.md`](M18-bvi-checkpoint.md) | Build-vs-integrate evaluation: Sourcegraph `src` CLI, self-hosted CE, scip-mcp; must run before M16 |

## Dependency graph

```
M0 (store + cache + budget)
 ├─► M1 (scip adapter)
 │    ├─► M2 (symbol tool)
 │    ├─► M3 (usages tool)
 │    ├─► M4 (edit symbol)
 │    ├─► M8 (call graph)
 │    └─► M9 (external deps)
 ├─► M5 (ast-grep pattern)     ← independent of SCIP
 ├─► M6 (semantic rank)        ← layered over M1 output
 ├─► M7 (recall_symbol)        ← needs M2
 ├─► M10 (multi-repo)          ← independent
 ├─► M11 (bootstrap)           ← runs all of the above on first context
 ├─► M12 (token budget)        ← woven into M0; sharpened after M2/M5
 ├─► M13 (docs)                ← last, depends on M2/M4/M5
 ├─► M14 (git history)         ← needs M0; uses M1 only for symbol-id resolution
 │    └─► M15 (blame/temporal) ← needs M14 + M1 (SCIP byte ranges)
 ├─► M17 (cross-lang edges)    ← needs M1 per-language indexes + M5 (ast-grep)
 └─► M18 (bvi checkpoint)      ← 2-day eval; must run before M16
      └─► M16 (zoekt scale)    ← only if M18 validates building it
```

Note: M1 also introduces `SymbolIntelStore` / `SymbolIntelProvider` (the routing layer and backend interface) because it's the first milestone with a second backend. M6/M10/M14/M16/M17 all register providers against the store that M1 builds. See `grounding.md` and the M1 milestone file for the interface definition.

Recommended build order: **M0 → M1 → M2 → M5 → M12 → M4 → M3 → M6 → M7 → M8 → M14 → M15 → M18 → M16 → M17 → M11 → M9 → M10 → M13**.

M14/M15 deliberately slot in before M11 so the symbol graveyard and blame index are warm by the time the first-context bootstrap job runs. M18 runs before M16 so we don't build Zoekt integration if Sourcegraph `src` CLI or a production scip-mcp already covers it.

## Cost-reduction principles (apply to every milestone)

These are the load-bearing decisions. Any milestone that violates them needs
explicit justification in its file.

1. **Outline before body.** Default response is signatures + 1-line summaries.
   Bodies fetched on explicit ask. Saves 80–95% of tokens on navigation.
2. **Content-addressed cache.** Every retrieval result keyed by
   `hash(query + index_version + repo_id)`. Cache hit returns immediately
   with `provenance="cached"`. No subprocess, no LLM round-trip.
3. **Token budgets are mandatory.** Every tool takes `budget_tokens`; the
   store packs the highest-information-density payload that fits. Bigger
   payloads require explicit caller intent.
4. **Stable IDs.** Symbol IDs are content-hashed (kind+qualified_name+
   signature), not position-based. Renames and reformats do not invalidate
   cached results.
5. **One trace per logical operation.** A symbol lookup that hits cache, runs
   ast-grep, and tags a memory block records one trace, not three.
6. **Memory always feeds the prefetch.** What the agent touched last session
   is pre-warmed into the cache before the first tool call.
7. **No live LSP in the hot path.** LSP is the *fallback for languages
   without a SCIP indexer*, not the primary mechanism. Period.

## Validation gates (cross-milestone)

Before any milestone is marked `completed`:

- New/changed rows added to `docs/agent-os/validation-matrix.md`.
- Unit tests under `tests/` for the milestone's slice.
- A token-cost benchmark recorded in `tests/benchmarks/code_intel/`
  comparing the new path against the prior baseline on the same task.
- A trace recorded via `mcp__atelier__record` referencing the milestone file.

## Out of scope (deliberately)

- Serena. We adopt SCIP (the artifact) not LSP-per-session (the protocol).
- Live language servers as the primary path. Optional fallback only.
- Replacing `mcp__atelier__search` — text/regex search stays for the cases
  where symbol lookup isn't the right tool.
- CodeQL/Semgrep — ast-grep covers ~80% of the value at ~10% of the
  complexity. Revisit if we add a security-review agent.
- IDE plugins. We are an MCP server.
- Full cross-language coverage. M17 ships ctypes/cffi + subprocess + dynamic
  import edges (~60% of real-world cross-lang refs); JNI, Rust FFI, and
  runtime-traced edges are out of scope — they require runtime analysis, not
  static.
- Build-graph awareness (Bazel/Buck deps as first-class index edges).
  Requires per-build-system adapters; revisit if a user repo demands it.
- Megarepos >50M LOC. Zoekt (M16) covers up to ~50M LOC. Beyond that needs
  Google-internal-style sharded infra.
