# Phase 18 Validation — Tree-sitter Repo-map Tags

## Focused checks

- `uv run pytest tests/infra/tree_sitter/test_tags.py tests/core/test_repo_map.py -q`
- `uv run pytest tests/infra/code_intel/git_history/test_graveyard.py -q`
- `uv run ruff check src/atelier/infra/tree_sitter/tags.py src/atelier/core/capabilities/semantic_file_memory/treesitter_ast.py tests/infra/tree_sitter/test_tags.py tests/core/test_repo_map.py`
- `uv run mypy --strict src/atelier/infra/tree_sitter/tags.py src/atelier/core/capabilities/semantic_file_memory/treesitter_ast.py`

## Acceptance criteria

- Every language in the tree-sitter outline support set routes through tree-sitter tag extraction.
- Java/Ruby/etc. fixtures now produce definition tags where regex previously produced none.
- JSON/YAML/TOML emit bounded definition tags and no references.
- Parser-missing non-legacy languages do not fall through to JavaScript regex.
- Repo-map can rank a previously unsupported language via extracted definition tags.
- Existing Python deleted-blob tag extraction remains supported.

## Known external blocker

Full repository gates currently have unrelated dirty-worktree failures outside this phase. Do focused validation for Phase 18 and do not patch unrelated files just to force global green.
