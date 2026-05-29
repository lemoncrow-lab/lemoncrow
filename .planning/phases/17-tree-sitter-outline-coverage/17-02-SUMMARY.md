---
phase: 17-tree-sitter-outline-coverage
plan: 02
subsystem: code-intel
tags: [tree-sitter, outline, smart_read, sql, yaml, json, semantic-file-memory]

# Dependency graph
requires:
  - phase: 17-tree-sitter-outline-coverage
    plan: 01
    provides: "Generalized outline_text engine (unwrap descent + keep_first_line emit) + LangCfg fields"
  - phase: 16-language-registry
    provides: "canonical sql/yaml/json keys via language_by_name"
provides:
  - "sql _LANG_CONFIG entry (unwrap statement; signature-trim schema constructs)"
  - "yaml _LANG_CONFIG entry (3-level wrapper descent; top-level keys only)"
  - "json _LANG_CONFIG entry (document/object descent; top-level pairs, guard-gated)"
affects: [code-intel, smart_read]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "unwrap descent reaches declarations buried in transparent wrapper nodes (statement / stream→document→block_node→block_mapping / document→object)"
    - "keep_first_line surfaces top-level data keys while dropping nested values"
    - "25% savings guard in capability.smart_read remains the single authority; small/flat JSON degrades by design"

key-files:
  created:
    - tests/core/test_sql_outline.py
    - tests/core/test_yaml_outline.py
    - tests/core/test_json_outline.py
  modified:
    - src/atelier/core/capabilities/semantic_file_memory/treesitter_ast.py

key-decisions:
  - "sql uses keep_signature (not keep_full) for CREATE constructs so bodies are stripped — clears guard at ~67%"
  - "json/yaml use keep_first_line on pair/block_mapping_pair so only top-level keys emit; nested values terminate and are dropped"
  - "No engine or guard changes — only the _LANG_CONFIG table grew (3 entries); SUPPORTED_LANGUAGES auto-derives"
  - "Small/flat JSON degradation is designed DLS-OUTLINE-05 behavior, not a bug; verified with a compact fixture (~96% ratio) the guard correctly rejects"

requirements-completed: [DLS-OUTLINE-02, DLS-OUTLINE-03, DLS-OUTLINE-05]

# Metrics
duration: ~15min
completed: 2026-05-29
---

# Phase 17 Plan 02: SQL / YAML / JSON Tree-sitter Outline Coverage Summary

**Added three descent-dependent `_LANG_CONFIG` entries — `sql`, `yaml`, `json` — leveraging the 17-01 `unwrap`/`keep_first_line` engine generalization so schema-level SQL constructs, top-level YAML document keys, and large nested JSON structure reach the tree-sitter outline path, while small/flat JSON degrades cleanly via the untouched 25% savings guard.**

## Performance

- **Duration:** ~15 min
- **Tasks:** 2 completed
- **Files modified:** 4 (3 created, 1 modified)

## Accomplishments
- `sql`: `unwrap={statement}`, `keep_signature={create_table, create_view, create_index, create_function, alter_table}`, `body_kinds={column_definitions, function_body, create_query, index_fields}` — all four schema construct types are surfaced as signatures with bodies stripped (DLS-OUTLINE-02).
- `yaml`: `unwrap={stream, document, block_node, block_mapping}`, `keep_first_line={block_mapping_pair}` — top-level document keys only; deeply nested mappings/scalars dropped (DLS-OUTLINE-03).
- `json`: `unwrap={document, object}`, `keep_first_line={pair}` — large nested JSON clears the guard → treesitter; small/flat JSON rejected by the 25% guard → generic/full (designed DLS-OUTLINE-05 degradation).
- No engine or guard modifications: `capability.smart_read`'s `len(text) <= 0.75 * len(source)` guard stays the single authority; `capability.py` and `languages.py` untouched. `SUPPORTED_LANGUAGES` auto-derives from `_LANG_CONFIG.keys()`.

## Task Commits

1. **Task 1: Add failing SQL/YAML/JSON outline tests** — `ddf43bc` (test) — RED: 3 treesitter-asserting tests fail (no config yet); small-flat JSON degradation test passes.
2. **Task 2: Add sql/yaml/json `_LANG_CONFIG` entries** — `9ae6c91` (feat) — GREEN: all 4 tests pass.

## Files Created/Modified
- `tests/core/test_sql_outline.py` (created) — full-pipeline `smart_read` test; asserts `language == "sql"`, `mode == "outline"`, `kind == "treesitter"`, all four construct names present, function-body token absent.
- `tests/core/test_yaml_outline.py` (created) — asserts `language == "yaml"`, `kind == "treesitter"`, top-level keys present, deeply-nested scalar absent.
- `tests/core/test_json_outline.py` (created) — two tests documenting DLS-OUTLINE-05: large-nested → treesitter; small-flat (compact) → guard degradation (generic or full).
- `src/atelier/core/capabilities/semantic_file_memory/treesitter_ast.py` (modified) — added three `_LANG_CONFIG` entries (sql/yaml/json). Engine + guard unchanged.

## Deviations from Plan

**1. [Rule 1 - Bug] JSON degradation fixture refined to actually exercise the guard**
- **Found during:** Task 2
- **Issue:** The original small-flat JSON fixture (5 keys, indented `{ "a": 1, ... }`) was *trimmable*: its indentation/whitespace meant the tree-sitter outline came out at ~65% of source, so it *cleared* the 25% guard and returned `treesitter` — failing the degradation assertion. The fixture did not represent a genuinely dense, non-trimmable file.
- **Fix:** Replaced with a compact single-line flat JSON (longer string values, no trimmable indentation) whose outline is ~96% of source, so the guard correctly rejects it → generic/full. This is the designed DLS-OUTLINE-05 degradation the test is meant to prove.
- **Files modified:** `tests/core/test_json_outline.py`
- **Commit:** `9ae6c91` (bundled with Task 2 config since it adjusts the test's fixture, not the config behavior)

The pre-commit hook auto-formatted `treesitter_ast.py` once (cosmetic); re-staged and committed without further code changes.

## Threat Model Coverage
- **T-17-03 (fake savings / Information Disclosure):** Mitigated — `capability.smart_read`'s `len(text) <= 0.75*len(source)` guard left untouched and authoritative; small/flat JSON correctly degrades to generic/full (validated by `test_json_small_flat_degrades_via_guard`).
- **T-17-04 (DoS via unbounded recursion):** Mitigated — `visit()` recurses only into finite `unwrap` kinds, never into kept nodes; `parser.parse` wrapped in try/except returning `None`.
- **T-17-SC (package installs / Tampering):** Accepted — no installs; grammars ship in vetted `tree-sitter-language-pack` 1.8.1.

## Known Stubs
None.

## Validation Run
- `uv run pytest tests/core/test_sql_outline.py tests/core/test_yaml_outline.py tests/core/test_json_outline.py -q` → 4 passed
- `uv run pytest tests/core -k outline -q` → 13 passed, 784 deselected
- `uv run pytest tests/core/test_shell_outline.py tests/core/test_sql_outline.py tests/core/test_yaml_outline.py tests/core/test_toml_outline.py tests/core/test_json_outline.py tests/core/test_rust_outline.py -q` → 7 passed
- `uv run ruff check src/.../treesitter_ast.py tests/core/test_sql_outline.py tests/core/test_yaml_outline.py tests/core/test_json_outline.py` → All checks passed
- `uv run mypy --strict src/.../treesitter_ast.py` → Success: no issues found

## Notes on Repository Gate
The worktree contains numerous unrelated user changes/deletions (e.g. `tests/core/test_phase_runner*.py`, `test_runtime_mode_dispatch.py`, benchmark/docs deletions). Per plan constraints, these were NOT touched. The full `make lint && make typecheck && make test` repository gate was not run to completion here because such unrelated user WIP may produce failures outside the Phase 17 change surface (`treesitter_ast.py` + `tests/core/*_outline.py`). All Phase 17 focused gates pass cleanly.

## Self-Check: PASSED
- `tests/core/test_sql_outline.py` — FOUND
- `tests/core/test_yaml_outline.py` — FOUND
- `tests/core/test_json_outline.py` — FOUND
- `src/atelier/core/capabilities/semantic_file_memory/treesitter_ast.py` — FOUND (modified)
- Commits `ddf43bc`, `9ae6c91` — FOUND in git log
