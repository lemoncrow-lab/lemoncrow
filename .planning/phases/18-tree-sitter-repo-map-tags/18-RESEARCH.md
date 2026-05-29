# Phase 18 Research — Tree-sitter Repo-map Tags

## Source

`docs/plans/dedicated-language-support/M3-treesitter-tags.md`

## Findings

- `src/atelier/infra/tree_sitter/tags.py` currently uses Python `ast` plus regexes for JavaScript, TypeScript, Go, and Rust.
- `detect_language()` now delegates to the canonical registry, so languages without a regex fall through to the JavaScript regex default unless routing changes.
- `src/atelier/core/capabilities/semantic_file_memory/treesitter_ast.py` already centralizes tree-sitter parser access and per-language definition-like node kinds in `_LANG_CONFIG`.
- Installed parsers are available for the outline-supported languages: Go, Rust, Java, Ruby, C, C++, C#, Kotlin, PHP, Swift, Scala, Bash, TOML, SQL, YAML, and JSON.
- Data-language definitions must be bounded and definition-only. Emitting identifier references for JSON/YAML/TOML would create noisy repo-map edges for common keys like `name`, `type`, and `version`.
- SQL grammar nodes verified locally:
  - `create_table` uses `object_reference > identifier`
  - `create_index` uses a direct `identifier`
  - `create_view` uses `object_reference > identifier`
- Bash grammar nodes verified locally:
  - `variable_assignment` has `variable_name`
  - `function_definition` uses `word` for function name

## Decisions

- Keep Python on stdlib `ast`.
- Route every language in `SUPPORTED_LANGUAGES` through a new tree-sitter tagger.
- Expose public helper(s) from `treesitter_ast.py` for definition node kinds and parser access; do not import `_LANG_CONFIG` directly from `tags.py`.
- Preserve existing regex extraction only for legacy JavaScript, TypeScript, Go, and Rust fallback when parser support is unavailable or parsing fails.
- For non-legacy tree-sitter languages, parser-missing/parse-failed cases return `[]` rather than JavaScript-regex garbage.
- Emit references only for code languages where identifier references are useful; emit definitions only for JSON/YAML/TOML, and no broad SQL/YAML/JSON references.

## Risks

- Tree-sitter node shapes vary by grammar package; tests must pin expected names for representative fixtures.
- Repo-map graph construction links references to matching definitions by plain name, so overly broad data-language references would degrade PageRank.
- `iter_source_files()` still defaults to Python/JS/TS/Go/Rust globs; Phase 18 only needs graph behavior verified with explicit files. Broad discovery expansion belongs with later indexing/validation phases.
