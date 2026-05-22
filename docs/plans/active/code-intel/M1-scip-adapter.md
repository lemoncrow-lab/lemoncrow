# M1 — SCIP backend behind `CodeContextEngine`

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Blocked by M0. Blocks M3, M4, M8, M9. (M2 ships without SCIP via the
> existing local backend; SCIP is a drop-in speed-up once available.)

**No MCP surface change.** The SCIP backend is called *from inside*
`CodeContextEngine.search_symbols / get_symbol / find_references / …`. Agents
keep calling `tool_code` exactly as today; results get faster and more
accurate when SCIP is warm.

## Goal

Adopt SCIP as the primary symbol-intel artifact. Precomputed, microsecond
queries, no live LSP, no per-session subprocess. This is the keystone of the
whole plan — every name lookup, reference walk, and call hierarchy traversal
goes through here first.

## Background — why SCIP, in one paragraph

SCIP (Sourcegraph Code Intelligence Protocol) is a binary protobuf format that
records every symbol definition, reference, hover, and relationship in a repo
into a single file. Indexers exist for Python, TypeScript, Go, Rust, Java,
C#, Ruby, and more. Index once with `scip-python` (etc.), get a `.scip` file,
query it locally with sub-millisecond reads. This is the format GitHub uses
for Precise Code Navigation and what Sourcegraph standardized on after LSIF.
LSP per session cannot compete on cost.

References: <https://github.com/sourcegraph/scip>, <https://about.sourcegraph.com/blog/announcing-scip>.

## Module layout

```
src/atelier/infra/code_intel/scip/
  __init__.py
  AGENT_README.md
  indexer.py           Subprocess-runs scip-<lang>; manages binary discovery
  reader.py            Parses .scip protobuf, exposes typed query API
  watcher.py           Detects file changes, schedules incremental reindex
  adapter.py           SymbolIntelProvider implementation
  binaries.py          Resolves which scip-<lang> binary to use per file
  proto/               Vendored .proto + generated Python (do NOT edit by hand)

src/atelier/core/capabilities/code_context/
  intel_store.py       NEW — SymbolIntelStore (routing layer) + SymbolIntelProvider (interface)
```

### `SymbolIntelStore` and `SymbolIntelProvider` (NEW in this milestone)

M0 introduced cache + budget primitives but no routing layer — there was only
one backend. M1 is the first milestone where two backends coexist (local
`CodeContextEngine` path and the new SCIP adapter), so the routing layer lands
here and becomes the chokepoint that M6, M10, M14, M16, and M17 plug into.

```python
# src/atelier/core/capabilities/code_context/intel_store.py
from typing import Protocol

class SymbolIntelProvider(Protocol):
    """Backend interface implemented by SCIP (M1), embeddings (M6),
    graveyard (M14), zoekt (M16), cross-lang (M17)."""
    name: str                                # "scip" | "graveyard" | "zoekt" | ...
    def health(self) -> ProviderHealth: ...  # ok/degraded/unhealthy
    def find_symbol(self, query: str, **kwargs) -> list[SymbolHit]: ...
    # additional methods added by later milestones via Protocol extension

class SymbolIntelStore:
    """Routes queries to the best available provider.
    Composes with M0's RetrievalCache + BudgetPacker before returning."""
    def __init__(self, engine: CodeContextEngine,
                 cache: RetrievalCache, packer: BudgetPacker): ...
    def register(self, provider: SymbolIntelProvider) -> None: ...
    def find_symbol(self, query, *, budget_tokens, **kwargs): ...
    # Routing rule for M1: SCIP if healthy, else fall back to engine.LocalAdapter
```

Later milestones extend `SymbolIntelProvider` with their own methods (e.g.,
M14 adds `find_deleted`, M15 adds `annotate`, M16 adds text search). M10
adds multi-repo composition over the same store. The store is constructed
once per `CodeContextEngine` and held in engine session state — `tool_code`
calls go through `engine` → `store` → `provider`, never directly to a provider.

## Indexer (`indexer.py`)

- Detects languages present in the repo via existing
  `repo_map/graph.iter_source_files`.
- For each language, runs the matching SCIP indexer as a subprocess. Output:
  `.atelier/cache/scip/<repo_id>/<lang>.scip`.
- Indexer binaries are installed lazily via a small `scip-installer` helper
  (downloads released static binaries; no `cargo`/`npm` build step needed).
- Indexers run **in a background job** via the existing
  `core/service/jobs.py` worker. First call may return `provenance="local"`
  results from `LocalAdapter` while SCIP bakes; subsequent calls hit SCIP.

Indexer binaries to bundle/install on first use:

| Language | Binary | Source |
|---|---|---|
| Python | `scip-python` | npm `@sourcegraph/scip-python` |
| TypeScript/JS | `scip-typescript` | npm `@sourcegraph/scip-typescript` |
| Go | `scip-go` | `go install github.com/sourcegraph/scip-go` |
| Rust | `rust-analyzer scip` | rust-analyzer ships `scip` subcommand |
| Java | `scip-java` | coursier |
| Ruby | `scip-ruby` | gem |
| C# | `scip-dotnet` | dotnet tool |

## Reader (`reader.py`)

- Parses the `.scip` protobuf using vendored generated code.
- Builds in-memory indexes for fast lookup:
  - `name → SymbolHit[]` (FTS over symbol names + qualified names)
  - `symbol_id → definition position`
  - `symbol_id → list[reference position]`
  - `symbol_id → list[call edge]`
- Memory footprint: ~5-10 MB per 100k symbols. Atelier repo ~50k symbols → ~5 MB.
- Reader is process-local; SCIP files are read-only artifacts so we can mmap.

## Watcher (`watcher.py`)

- Uses `watchdog` (or `inotify` directly on Linux) to detect source file
  changes.
- Debounces, then schedules an incremental reindex of the affected language
  only.
- Bumps `index_version` in the cache so stale `RetrievalCache` entries
  invalidate.
- Reindex is non-blocking; reader keeps serving old index until new one swaps
  atomically.

## Adapter (`adapter.py`)

Implements `SymbolIntelProvider`:

| Method | Implementation |
|---|---|
| `health` | Returns `ok=True` if at least one `.scip` file exists for the repo; latency is the time to do a 1-symbol lookup. |
| `find_symbol` | Reader FTS over names. Engine wraps results with `provenance="scip"`. |
| `find_references` | Reader lookup `symbol_id → references`. Microseconds. |
| `call_hierarchy` | Reader walks the call-edge graph to depth. |
| edits | **Not implemented** — SCIP is read-only. Edits go through M4's `kind="symbol"` rich-edit descriptor, which calls `engine.get_symbol` (resolved via SCIP when available) then writes via the existing rich-edit path. After write, the watcher triggers an incremental SCIP reindex. |

## Validation

Unit tests under `tests/infra/code_intel_bridges/scip/`:

- `test_indexer_python_fixture` — small Python fixture → `.scip` exists, parses, finds known symbols.
- `test_reader_finds_symbol_in_microseconds` — assert lookup latency < 1ms on a pre-built fixture index.
- `test_watcher_invalidates_cache_on_edit` — edit a file → cache version bumps within debounce window.
- `test_adapter_health_false_when_no_scip` — empty cache dir → health.ok = False; store falls through to local.
- `test_references_match_lsp_baseline` — compare `find_references` output to live LSP on a fixture; must be ≥ LSP set.

Benchmark under `tests/benchmarks/code_intel/`:

- `bench_scip_vs_local.py` — same query repeated 1000 times against both adapters; SCIP must be ≥ 100× faster after first call.

## Exit criteria

- SCIP indexer runs for Python and TypeScript out-of-the-box on the Atelier repo itself.
- `SymbolIntelStore.find_symbol("CodeContextEngine")` returns the hit with `provenance="scip"` in < 5ms after a warm index.
- Token cost benchmark (in `tests/benchmarks/`) shows ≥ 50% reduction vs the `LocalAdapter`-only baseline on a 20-task navigation suite.

## Open questions

- **Bundle vs install** — ship SCIP binaries with Atelier or fetch on first use? Lean fetch-on-first-use with a checksum allowlist (smaller install, predictable provenance).
- **Multi-version** — what if the project uses Python 3.11 and 3.12? `scip-python` accepts a `--python-version` flag; default to the active venv.
- **External deps in SCIP** — `scip-python` and `scip-typescript` can index `site-packages`/`node_modules` if asked. Defer to M9.
- **Languages without a SCIP indexer** — `LocalAdapter` (tree-sitter + LSP) remains the fallback. No action needed in this milestone.
