# Grounding: existing MCP surface this plan extends

> Read this **before** any milestone file. Every milestone below extends an
> already-registered MCP tool in
> `src/atelier/gateway/adapters/mcp_server.py`. We do **not** introduce new
> top-level `@mcp_tool` entries unless a milestone explicitly says so.

## Current MCP tools (as of 2026-05-18)

Source: `src/atelier/gateway/adapters/mcp_server.py`. Registrations are
`@mcp_tool(name=…)` decorators.

| Tool name (`mcp__atelier__…`) | Function | Line | Already exposes |
|---|---|---|---|
| `context` | `tool_get_context` | 663 | ReasonBlock recall, task framing |
| `route` | `tool_route` | 737 | Routing decisions |
| `rescue` | `tool_rescue_failure` | 845 | Failure recovery |
| `trace` | `tool_record_trace` | 927 | Trace recording |
| `verify` | `tool_run_rubric_gate` | 1187 | Rubric gate execution |
| `memory` | `tool_memory` | 1381 | `block_upsert`, `block_get`, `archive`, `recall`, `transcript_recall`, `summarize` |
| `read` | `tool_smart_read` | 1450 | Outline-first read, `cache_hit`, `tokens_saved` |
| `edit` | `tool_smart_edit` | 1533 | Batch + rich edits, atomic, diff-recording |
| `sql` | `tool_sql` | 1574 | SQL over telemetry |
| `code` | `tool_code` | 1915 | `index`, `search`, `symbol`, `outline`, `context`, `impact` |
| `search` | `tool_smart_search` | 2007 | Smart search + native ripgrep/glob; `budget_tokens` |
| `compact` | `tool_compact` | 2125 | Conversation compaction |
| `shell` | `tool_shell` | 2189 | Sandboxed shell |

## Already in `code` tool — read this carefully

```python
@mcp_tool(name="code", is_dev=True)
def tool_code(
    op: Literal["index","search","symbol","outline","context","impact"],
    ...
)
```

- `op="index"` → `CodeContextEngine.index_repo(...)`
- `op="search"` → `engine.search_symbols(query, limit, kind, language)` — **this is already a name-first symbol lookup**
- `op="symbol"` → `engine.get_symbol(symbol_id|qualified_name|symbol_name|file_path)` — single-symbol details
- `op="outline"` → `engine.file_outline(file_path, limit)`
- `op="context"` → `engine.context_pack(task, seed_files, budget_tokens, max_symbols)` — **already budget-aware**
- `op="impact"` → `engine.impact(file_path)`

The engine (`CodeContextEngine`) already has every method we need for M2–M4
(`search_symbols`, `get_symbol`, `file_outline`, `context_pack`, `impact`,
`changed_symbols`, and an internal reindex path).

M0 adds the shared retrieval cache and budget packer under
`core/capabilities/code_context/`, then routes `tool_code` through engine-level
tool wrappers that stamp `cache_hit`, `tokens_saved`, and `provenance` on every
existing `code` op response.

**What's missing on the `code` tool:**
- `op="usages"` (M3) — wraps engine `search_symbols` + LSP/SCIP references
- `op="callers"` / `op="callees"` (M8) — call graph
- `op="pattern"` (M5) — ast-grep structural search/rewrite
- `op="recall"` (M7) — symbol↔memory fusion

**What's missing inside the engine:**
- SCIP backend behind `CodeContextEngine` query methods (M1)
- Function-level embedding storage + hybrid rank (M6)
- External dep indexing (M9)
- Multi-repo (M10)

## Already in `edit` tool

```python
@mcp_tool(name="edit", is_dev=True)
def tool_smart_edit(edits: list[dict[str, Any]], atomic: bool = True)
```

- Accepts a list of edit descriptors.
- Two paths: legacy `op` descriptors (`apply_batch_edit`) and rich descriptors
  with `file_path` / cell ops (`apply_rich_edits`).
- Snapshots → applies → records unified diffs into the ledger.
- Atomic mode rolls back on any failure.

**What's missing:** a `kind="symbol"` rich-edit descriptor (M4). Implementation
adds it in `core/capabilities/tool_supervision/rich_edit.py`. **No new MCP
tool.**

## Already in `read` tool

```python
@mcp_tool(name="read", is_dev=True)
def tool_smart_read(path, file_path, range, expand, max_lines)
```

- Outline-first mode for large files via `SemanticFileMemoryCapability.smart_read`.
- Returns `cache_hit`, `tokens_saved`, `outline`, `content`, `range`.

**No M-level work needed.** The outline-first discipline (M12) cites this as
the existing reference implementation.

## Already in `memory` tool

```python
@mcp_tool(name="memory", is_dev=True)
def tool_memory(op: Literal["block_upsert","block_get","archive","recall",
                             "transcript_recall","summarize"], ...)
```

- Block metadata is a free-form dict — `symbol_id` tags fit without a schema change.
- Recall already accepts query + tags.

**What's missing for M7:** add `op="recall_symbol"` that internally resolves
the name via the `code` tool's `search`, then runs `memory.recall` filtered by
`metadata.symbol_id`. Same MCP entry, new op.

## Already in `search` tool

```python
@mcp_tool(name="search", is_dev=True)
def tool_smart_search(query, path, mode, ..., budget_tokens=2000, ...)
```

- Two backends: `smart_search` (ranked, content-aware) and `search_workspace`
  (native ripgrep/glob).
- `budget_tokens` already enforced.

**No M-level work needed.** It stays as the text-search complement to
symbol-search. M13 docs guide agents on when to use which.

## Where new internal modules land

| Module | Path | Used by |
|---|---|---|
| SCIP backend | `src/atelier/infra/code_intel/scip/` | M1 (called by `CodeContextEngine`) |
| ast-grep adapter | `src/atelier/infra/code_intel/astgrep/` | M5 (called by `tool_code` `op="pattern"`) |
| Symbol-vector store | `src/atelier/core/capabilities/code_context/embedding.py` | M6 (used inside `CodeContextEngine.search_symbols`) |
| Symbol-edit descriptor | `src/atelier/core/capabilities/tool_supervision/rich_edit.py` extension | M4 (used by `tool_smart_edit`) |
| Retrieval cache | `src/atelier/core/capabilities/code_context/cache.py` | M0/M12 (used by `tool_code` ops) |
| Budget packer | `src/atelier/core/capabilities/code_context/budget.py` | M0/M12 (used by `tool_code` ops) |

Note: **no `infra/code_intel_bridges/` directory** (that was speculative from
when we still planned a Serena bridge). Code intel lives inside the existing
`code_context` capability tree.

## Per-milestone landing map (read this with each Mn file)

| Milestone | MCP surface change | Internal change |
|---|---|---|
| M0 | None | New `code_context/cache.py` + `budget.py`; refactor `tool_code` ops to route through engine wrappers using them |
| M1 | None — SCIP is internal | New `infra/code_intel/scip/` module; `CodeContextEngine.search_symbols/get_symbol/...` queries SCIP when index present |
| M2 | None — `code op="search"` already exists; this milestone *hardens* defaults (snippet, ranking, provenance field) | Snippet rendering in engine response; provenance tagging |
| M3 | `code` gains `op="usages"` | Engine method `find_references(symbol_id_or_query)` |
| M4 | `edit` accepts new rich descriptor `kind="symbol"` | `rich_edit.py` adds symbol descriptor handler; calls `engine.get_symbol` for byte-range |
| M5 | `code` gains `op="pattern"` | New `infra/code_intel/astgrep/` adapter; lazy binary fetch |
| M6 | None — `code op="search"` gains `mode` parameter | Embedding column on symbol index; hybrid RRF in `search_symbols` |
| M7 | `memory` gains `op="recall_symbol"` (or `code op="recall"`; pick on claim) | Cross-call composition of code + memory recall |
| M8 | `code` gains `op="callers"` and `op="callees"` | Engine call-graph walk over SCIP edges |
| M9 | None — same tools, new `scope` field on `code op="search"` | External-dep indexer flags wired through `CodeContextEngine.index_repo` |
| M10 | None — same tools, new `repo` field where useful | Multi-root support in `CodeContextEngine`; `.atelier/workspace.toml` reader |
| M11 | None — uses `jobs.py` worker | First-context job pipeline |
| M12 | Audits defaults across `code`, `read`, `edit`, `search` for outline-first | Sharpens M0's cache + budget; new `code op="cache_status"` |
| M13 | Documentation only | None |
| M14 | New optional fields on `code op="search"`: `scope="deleted"`, `since=`, `touched_by=` | New `infra/code_intel/git_history/` module (walker, renames, graveyard, adapter); new `symbol_graveyard` table in the engine SQLite DB |
| M15 | `code` gains `op="blame"` | New `git_history/blame.py` (BlameAnnotator); new `symbol_blame_cache` table in the engine SQLite DB; wires `since`/`touched_by` filters from M14 to live SCIP results |
| M16 | None — backend swap inside `SymbolIntelStore`; response gains `backend` field on `mcp__atelier__search` | New `infra/code_intel/zoekt/` module (server, client, indexer, adapter, binary); auto-routes when `total_loc > 500k`; blocked by M18 |
| M17 | New `cross_lang_refs` field on `code op="symbol"` and `code op="usages"` responses | New `infra/code_intel/cross_lang/` module (edges, resolvers for ctypes/cffi/subprocess/dynamic_import); new `cross_lang_edges` table in the engine SQLite DB |
| M18 | None — evaluation only | No code; produces a decision memo appended to `M18-bvi-checkpoint.md` |

> **Implicit infrastructure not owned by M0.** M1, M6, M10, M14, M16, and M17 all
> refer to `SymbolIntelStore` (the routing layer) and `SymbolIntelProvider` (the
> backend interface). These are introduced **inside M1** when the SCIP adapter
> becomes the second backend that needs to coexist with the local engine path.
> M0 lands the cache + budget primitives only; the routing layer is M1's
> responsibility. Any milestone that references `SymbolIntelStore` before M1
> ships should be flagged in code review.

## How an implementing agent reads this plan

1. Open `index.md` for the master overview and dependency graph.
2. Open `grounding.md` (this file) to confirm what already exists.
3. Open the milestone file you're claiming (`Mn-*.md`).
4. If anything in the milestone file contradicts this grounding doc, **this
   doc wins** — update the milestone file as part of your work and note the
   correction in the commit message.
