# Phase 19 Research — Expanded SCIP Registry and Lazy Indexing

## Source

`docs/plans/dedicated-language-support/M4-scip-registry.md`

## Findings

- `src/atelier/infra/code_intel/languages.py` is the canonical language registry and currently stores `scip_indexer` as a bare fallback binary name.
- `src/atelier/infra/code_intel/scip/binaries.py` preserves explicit env-var strings for Python and TypeScript/JavaScript and only iterates Python + TypeScript.
- `src/atelier/infra/code_intel/scip/indexer.py` only discovers prebuilt `.scip` artifacts; it does not run indexers.
- `ScipIndexer.available_binaries()` returns `dict[str, Path]` keyed by language, so `c` and `cpp` can both point at the same `scip-clang` path.
- Phase 16 preserved legacy env-var names as an operator contract; Phase 19 must extend explicit maps without formulaic derivation or legacy drift.

## Decisions

- Keep `Language.scip_indexer` as a bare binary name; use `rust-analyzer` for Rust and keep the `scip` subcommand in SCIP-specific metadata.
- Add SCIP execution metadata in `binaries.py`, not in the canonical `Language` dataclass.
- Preserve `ATELIER_SCIP_PYTHON_BIN` and `ATELIER_SCIP_TYPESCRIPT_BIN` byte-identically.
- Extend explicit env vars for new languages:
  - `ATELIER_SCIP_GO_BIN`
  - `ATELIER_SCIP_RUST_BIN`
  - `ATELIER_SCIP_JAVA_BIN`
  - `ATELIER_SCIP_RUBY_BIN`
  - `ATELIER_SCIP_CLANG_BIN` for both `c` and `cpp`
- Add lazy opt-in index execution only; no automatic startup indexing.
- Run subprocesses with argv lists, pinned cwd, captured output, timeout, and no shell.
- Return explicit result objects instead of raising for ordinary missing-binary or missing-context cases.

## Risks

- Indexer CLI flags differ by language; isolate argv construction behind tested specs.
- Go/Rust/Clang may produce `index.scip` in a directory instead of accepting one output file path; tests should cover output normalization.
- Java and C/C++ require project context. Phase 19 should skip cleanly when required context is missing and leave provisioning to Phase 20.
