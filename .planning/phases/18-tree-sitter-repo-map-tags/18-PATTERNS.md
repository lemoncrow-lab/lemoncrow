# Phase 18 Patterns — Tree-sitter Repo-map Tags

## Existing patterns

- `tags.py` owns the public tag API:
  - `Tag`
  - `detect_language`
  - `extract_tags_from_text`
  - `extract_tags`
- `treesitter_ast.py` owns tree-sitter language configuration and parser loading.
- Repo-map graph construction is a consumer only; it expects `Tag` records with stable `kind`, `name`, `file`, `line`, and `byte_range`.
- Existing tests for tag extraction live in `tests/core/test_repo_map.py`; Phase 18 adds dedicated infra-level tag tests while preserving current core tests.

## Implementation pattern

- Add narrow public helpers in `treesitter_ast.py`:
  - parser lookup by language
  - configured definition node kinds by language
  - supported tree-sitter language set
- Implement `_treesitter_tags(path, text, language)` in `tags.py`.
- Reuse tree-sitter byte offsets for `Tag.byte_range`; compute line numbers from byte offsets.
- Use grammar-aware name extraction:
  - identifier-like descendants for code languages
  - key/table/object-reference extraction for data and SQL languages
- Preserve fallback boundaries:
  - Python: `ast`
  - legacy regex languages: parser first, regex fallback
  - configured non-legacy languages: parser first, `[]` fallback

## Test pattern

- Unit-test definitions for previously unsupported code languages.
- Unit-test data languages for definition-only behavior.
- Unit-test parser-missing fallback for a data language.
- Add a graph smoke test that a previously unsupported language produces rankable symbols.
- Add a graph guard ensuring YAML common keys do not create noisy reference edges.
