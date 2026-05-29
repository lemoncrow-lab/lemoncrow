# Phase 21 Patterns — Validation, Benchmarks, and Docs

## Existing Patterns

| Need | Existing analog | Pattern |
|------|-----------------|---------|
| Language detection | `src/atelier/infra/code_intel/languages.py` | Use `LANGUAGES`, `language_for_path()`, and canonical names. |
| Outline behavior | `tests/core/test_shell_outline.py`, `tests/core/test_json_outline.py` | Use `SemanticFileMemoryCapability.smart_read(..., outline_threshold=0)` for functional tests. |
| Tag behavior | `tests/infra/tree_sitter/test_tags.py` | Use `extract_tags_from_text()` and filter `Tag.kind == "definition"`. |
| Savings measurements | `tests/benchmarks/test_read_ab_real.py` | Mark benchmarks with `@pytest.mark.ab`; use tiktoken when available; assert honest invariants. |
| SCIP registry/provisioning | `tests/infra/code_intel/scip/test_scip_registry.py` | Test statuses and managed discovery without relying on machine-global binaries. |
| Committed report artifacts | `reports/2026-W20/benchmark.json`, `reports/index.json` | Add JSON/Markdown artifact and index entry for the DLS benchmark. |
| Docs | `docs/architecture/README.md`, `README.md`, `QUICK_REFERENCE.md`, `docs/installation.md` | Document real support tiers and provisioning behavior. |

## Pitfalls

- Do not assert every supported language returns `mode == "outline"` for tiny fixtures; the 25% guard can return full/generic output.
- Do not treat SCIP availability as indexing success; C/C++ still need `compile_commands.json`, Rust/Java need user toolchains.
- Do not add new language maps; extend fixtures and tests around the canonical registry.
- Do not add Makefile wrappers for this phase; use direct `uv run` validation commands.

