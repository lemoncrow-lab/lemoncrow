# M17 — Cross-language edge resolution (partial)

> Parent: [`index.md`](index.md). Grounding: [`grounding.md`](grounding.md).
> Blocked by M1 (SCIP per-language indexes must exist before cross-referencing
> them). Independent of M14–M16.
> **Partial solution only** — see honest ceiling below.

**No new MCP tool.** Adds a cross-language edge table to the existing
`CodeContextEngine` SQLite DB. Results surface as `cross_lang_refs` on
`code op="symbol"` and `code op="usages"` responses.

## Goal

When an agent finds a symbol in Python that calls into a C extension, or a
TypeScript module that spawns a Python subprocess, the current stack returns
no information about the target. This milestone adds a best-effort cross-
language edge resolver that covers the most common FFI patterns in web and ML
codebases — the patterns that account for ~60% of cross-language references in
practice.

## Honest ceiling

Full cross-language resolution at Google's level requires indexer cooperation
and runtime analysis (e.g., Java↔C via JNI, Rust FFI with `#[no_mangle]`).
The public SCIP indexers do not emit cross-language edges. This milestone
targets the achievable subset using static analysis only:

| Tier | Pattern | Languages | Coverage | Implemented |
|---|---|---|---|---|
| 1 | `ctypes.CDLL` / `cffi.FFI.cdef` | Python → C | ~80% of Python/C FFI | ✅ this milestone |
| 1 | `ctypes.cdll.LoadLibrary` name matching | Python → C | as above | ✅ this milestone |
| 2 | Subprocess call to known Python script | TypeScript/Go → Python | ~50% of ML inference bridges | ✅ this milestone |
| 2 | `importlib.import_module` dynamic import | Python → Python | cross-package boundaries | ✅ this milestone |
| 3 | JNI (`native` methods, `System.loadLibrary`) | Java → C | common in Android | ❌ deferred |
| 3 | Rust `extern "C"` + `#[no_mangle]` | Rust → C | common in embedded | ❌ deferred |
| 3 | WASM imports | any → WASM | rare | ❌ out of scope |

Tier 3 requires runtime tracing or JVM/LLVM IR analysis — not practical with
static analysis alone.

## Module layout

```
src/atelier/infra/code_intel/cross_lang/
  __init__.py
  AGENT_README.md
  edges.py         CrossLangEdgeDB — SQLite table + upsert + query
  resolvers/
    ctypes_resolver.py       Tier 1: ctypes.CDLL / cffi
    subprocess_resolver.py   Tier 2: subprocess.run / Popen with .py target
    dynamic_import.py        Tier 2: importlib.import_module
  runner.py        Orchestrates all resolvers over repo; called from M11
```

## Cross-language edge table

```sql
CREATE TABLE IF NOT EXISTS cross_lang_edges (
    id                INTEGER PRIMARY KEY,
    src_symbol_id     TEXT NOT NULL,      -- SCIP symbol id (caller side)
    src_language      TEXT NOT NULL,
    src_file_path     TEXT NOT NULL,
    src_line          INTEGER,
    tgt_symbol_name   TEXT NOT NULL,      -- best-effort; may not resolve to a SCIP id
    tgt_symbol_id     TEXT,               -- SCIP id if resolved, NULL if not
    tgt_language      TEXT NOT NULL,
    tgt_file_path     TEXT,               -- NULL if library not indexed
    edge_kind         TEXT NOT NULL,      -- "ffi_ctypes" | "ffi_cffi" | "subprocess" | "dynamic_import"
    confidence        REAL NOT NULL,      -- 0.0–1.0
    UNIQUE(src_symbol_id, tgt_symbol_name, edge_kind)
);
CREATE INDEX IF NOT EXISTS idx_cross_src  ON cross_lang_edges(src_symbol_id);
CREATE INDEX IF NOT EXISTS idx_cross_name ON cross_lang_edges(tgt_symbol_name);
```

## Tier 1: ctypes / cffi resolver (`ctypes_resolver.py`)

Uses ast-grep (M5 infrastructure) to find ctypes call sites:

```python
CTYPES_PATTERNS = [
    # ctypes.CDLL("libfoo.so")
    'ctypes.CDLL($LIB)',
    'ctypes.cdll.LoadLibrary($LIB)',
    'ctypes.WinDLL($LIB)',
    # After load: lib.foo_function(...)  ← see scope-binding note below
    '$LIB_VAR.$FUNC($ARGS)',
]
```

> **Scope binding caveat.** `$LIB_VAR.$FUNC($ARGS)` on its own matches every
> method call in the module — `requests.get`, `logger.info`, anything. To
> avoid drowning the cross-language table in false positives, the resolver
> performs a two-pass walk per file:
>
> 1. First pass collects every assignment of the form `X = ctypes.CDLL(...)`
>    or `X = cffi.FFI(...)`, building a set `ffi_handles = {X, Y, ...}` per
>    file (a flat per-file set, not full scope analysis).
> 2. Second pass walks `$LIB_VAR.$FUNC($ARGS)` matches and only emits an
>    edge when `$LIB_VAR ∈ ffi_handles`.
>
> Even with this, the per-file set is naive: it ignores shadowing,
> conditional reassignment, and cross-module handle passing. Realistic
> confidence for a Tier-1 edge is therefore **0.65** (not 0.85), and edges
> derived from cross-function handle flow drop to **0.45**. The 0.85
> confidence is reserved only for the direct `lib.func(...)` call on the
> same line as the CDLL assignment.

def resolve_ctypes(repo_root: str, scip_reader) -> list[CrossLangEdge]:
    edges = []
    for py_file in iter_python_files(repo_root):
        # Pass 1: collect ctypes handle variable names in this file
        handles = collect_ffi_handles(py_file)  # {"lib": "libfoo.so", ...}
        if not handles:
            continue
        # Pass 2: only emit edges for method calls on those handles
        for func_match in find_method_calls_on(handles.keys(), py_file):
            var = func_match.captures["LIB_VAR"]
            lib_name = handles[var]
            same_line = func_match.line == handles_assignment_line(var)
            c_hits = scip_reader.find_symbol(
                lib_name.replace("lib", "").replace(".so", "")
            )
            tgt_id = resolve_c_symbol(func_match.captures["FUNC"], c_hits)
            edges.append(CrossLangEdge(
                src_symbol_id=...,
                src_language="python",
                tgt_symbol_name=func_match.captures["FUNC"],
                tgt_symbol_id=tgt_id,   # None if C not indexed
                tgt_language="c",
                edge_kind="ffi_ctypes",
                confidence=(0.85 if same_line else 0.65) if tgt_id else 0.45,
            ))
    return edges
```

cffi resolution uses the same pattern with `ffi.cdef("""...""")` string
parsing to extract declared function names.

## Tier 2: subprocess + dynamic import resolver

```python
SUBPROCESS_PATTERNS = [
    'subprocess.run([$CMD, $SCRIPT, $$$ARGS])',
    'subprocess.Popen([$CMD, $SCRIPT, $$$ARGS])',
    'os.system($CMD)',
]

def resolve_subprocess(repo_root: str, scip_reader) -> list[CrossLangEdge]:
    for match in run_pattern(SUBPROCESS_PATTERNS[0], language="typescript"):
        script = match.captures.get("SCRIPT", "").strip("\"'")
        if not script.endswith(".py"):
            continue
        # Find Python entry point in that file
        py_hits = scip_reader.find_symbol("main", file_glob=script)
        edges.append(CrossLangEdge(
            src_language="typescript",
            tgt_symbol_name="main",
            tgt_file_path=script,
            tgt_symbol_id=py_hits[0].symbol_id if py_hits else None,
            tgt_language="python",
            edge_kind="subprocess",
            confidence=0.7,
        ))
    return edges
```

`dynamic_import.py` similarly walks `importlib.import_module("pkg.mod")` calls
and cross-references with the SCIP Python index.

## Surface on existing ops

### `code op="symbol"` — gains `cross_lang_refs` field

```json
{
  "symbol_name": "run_inference",
  "qualified_name": "src.inference.runner.run_inference",
  "file_path": "src/inference/runner.py",
  ...,
  "cross_lang_refs": [
    {
      "direction": "called_from",
      "src_language": "typescript",
      "src_file_path": "frontend/api/model.ts",
      "src_line": 42,
      "edge_kind": "subprocess",
      "confidence": 0.7
    }
  ]
}
```

### `code op="usages"` — includes cross-language callers

When M3's `find_references` is called, cross-language edges with `confidence
>= 0.5` are appended to the results with `provenance="cross_lang"`.

## Runner and bootstrap integration (M11)

```python
# M11 bootstrap job, step N:
from atelier.infra.code_intel.cross_lang.runner import CrossLangRunner
runner = CrossLangRunner(engine, scip_reader, astgrep_adapter)
runner.resolve_all(repo_root)   # runs all tier-1 and tier-2 resolvers
```

Runs after SCIP index is warm. Incremental: on file change (M1 watcher), only
re-run resolvers for the affected file's language.

## Validation

Tests under `tests/infra/code_intel/cross_lang/`:

- `test_ctypes_edge_found` — Python fixture calling `ctypes.CDLL("libfoo.so")`
  and then `lib.foo_compute(x)` → cross-lang edge with `tgt_symbol_name="foo_compute"`.
- `test_cffi_edge_found` — cffi `ffi.cdef("int bar(int x);")` → edge with
  `tgt_symbol_name="bar"`.
- `test_subprocess_py_edge_found` — TypeScript fixture with
  `subprocess.run(["python", "scripts/infer.py"])` → edge to `infer.py:main`.
- `test_low_confidence_when_unresolved` — ctypes call to unindexed library →
  edge with `confidence=0.5` and `tgt_symbol_id=None`.
- `test_cross_lang_ref_on_symbol_op` — `code(op="symbol", query="foo_compute")`
  includes cross-lang callers in the response.
- `test_usages_includes_cross_lang` — `code(op="usages", query="run_inference")`
  includes the TypeScript subprocess caller.

## Exit criteria

- Tier-1 (ctypes/cffi) resolver runs on the Atelier repo without error.
- Tier-2 (subprocess/dynamic import) resolver runs on the Atelier repo.
- Known cross-language edge in a fixture resolved with `confidence >= 0.7`.
- `code(op="symbol")` includes `cross_lang_refs` when edges exist.
- Validation matrix row added.

## Open questions

- **Confidence threshold for display.** Show edges with `confidence >= 0.5`
  but mark `< 0.7` as `"soft"` in the response. Let callers filter.
- **Tier 3 deferral note.** JNI, Rust FFI, and WASM are out of scope.
  Document clearly in `AGENT_README.md` so agents know not to expect them.
- **C SCIP indexer availability.** Tier 1 only resolves to a `tgt_symbol_id`
  if the C files are indexed. Most repos don't index C with SCIP; in that case
  we still emit the edge with `tgt_symbol_id=None` and `confidence=0.5`.
