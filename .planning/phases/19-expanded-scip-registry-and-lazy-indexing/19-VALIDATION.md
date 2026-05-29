# Phase 19 Validation — Expanded SCIP Registry and Lazy Indexing

## Focused checks

- `uv run pytest tests/infra/code_intel/scip -q`
- `uv run pytest tests/gateway/test_context_mcp_handler.py -q`
- `uv run ruff check src/atelier/infra/code_intel/languages.py src/atelier/infra/code_intel/scip/binaries.py src/atelier/infra/code_intel/scip/indexer.py tests/infra/code_intel/scip`
- `uv run mypy --strict src/atelier/infra/code_intel/languages.py src/atelier/infra/code_intel/scip/binaries.py src/atelier/infra/code_intel/scip/indexer.py`

## Acceptance criteria

- Canonical SCIP registry includes Python, TypeScript, JavaScript, Go, Rust, Java, Ruby, C, and C++.
- Legacy Python and TypeScript env vars remain byte-identical.
- `discover_scip_binaries()` iterates the supported registry instead of a hard-coded two-language tuple.
- Rust discovery uses `rust-analyzer` as the executable and `scip` as argv metadata.
- Lazy indexing is opt-in and writes artifacts under `default_scip_cache_root`.
- Missing binary or missing required context returns a non-success result without spawning unsafe commands.

## Known external blocker

Full repository gates currently have unrelated dirty-worktree failures outside this phase. Do focused validation for Phase 19 and do not patch unrelated files just to force global green.
