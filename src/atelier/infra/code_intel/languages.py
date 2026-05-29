"""Canonical language registry — the single source of truth for language identity.

This stdlib-only leaf module unifies the previously duplicated extension→language
maps (``semantic_file_memory.capability._language_for`` and
``tool_supervision.search_read._LANG_MAP``) into one frozen table. ``name`` is the
tree-sitter-language-pack parser name; ``parser_name`` usually equals ``name``.

Design constraints:

* Imports nothing from ``atelier.core`` (avoids the import cycle — Pitfall 1).
* Shell extensions canonicalize to ``bash`` at the data layer (DLS-LANG-03); the
  legacy ``"shell"`` spelling is intentionally not reproduced here.
* ``scip_indexer`` is seeded only for the three known indexers (python,
  typescript, javascript); the full SCIP table is deferred to Phase 19.
* This is a static table — no auto-detection machinery.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Language:
    """A canonical language record keyed by its tree-sitter parser name."""

    name: str
    extensions: frozenset[str]
    parser_name: str
    scip_indexer: str | None


LANGUAGES: tuple[Language, ...] = (
    Language("python", frozenset({".py", ".pyi"}), "python", "scip-python"),
    Language("typescript", frozenset({".ts", ".tsx"}), "typescript", "scip-typescript"),
    Language(
        "javascript",
        frozenset({".js", ".jsx", ".mjs", ".cjs"}),
        "javascript",
        "scip-typescript",
    ),
    # Shell extensions canonicalize to bash (DLS-LANG-03).
    Language("bash", frozenset({".sh", ".bash", ".zsh"}), "bash", None),
    # Canonical spelling is `csharp`, NOT `c_sharp`.
    Language("csharp", frozenset({".cs"}), "csharp", None),
    Language("go", frozenset({".go"}), "go", None),
    Language("rust", frozenset({".rs"}), "rust", None),
    Language("java", frozenset({".java"}), "java", None),
    Language("kotlin", frozenset({".kt", ".kts"}), "kotlin", None),
    Language("scala", frozenset({".scala"}), "scala", None),
    Language("ruby", frozenset({".rb"}), "ruby", None),
    Language("cpp", frozenset({".cpp", ".cc", ".cxx", ".hpp", ".hh"}), "cpp", None),
    Language("c", frozenset({".c", ".h"}), "c", None),
    Language("swift", frozenset({".swift"}), "swift", None),
    Language("php", frozenset({".php"}), "php", None),
    Language("sql", frozenset({".sql"}), "sql", None),
    Language("markdown", frozenset({".md", ".markdown"}), "markdown", None),
    Language("yaml", frozenset({".yaml", ".yml"}), "yaml", None),
    Language("toml", frozenset({".toml"}), "toml", None),
    Language("json", frozenset({".json"}), "json", None),
)


EXTENSION_TO_LANGUAGE: dict[str, Language] = {
    extension: language for language in LANGUAGES for extension in language.extensions
}

_BY_NAME: dict[str, Language] = {language.name: language for language in LANGUAGES}

ALL_LANGUAGES: frozenset[str] = frozenset(_BY_NAME)


def language_for_path(path: str | Path) -> Language | None:
    """Resolve a file path to its ``Language`` record, or ``None`` if unknown.

    Callers map ``None`` to ``"text"`` at their own boundary (DLS-LANG-02).
    """

    return EXTENSION_TO_LANGUAGE.get(Path(path).suffix.lower())


def language_by_name(name: str) -> Language | None:
    """Resolve a canonical language name to its ``Language`` record."""

    return _BY_NAME.get(name)


__all__ = [
    "ALL_LANGUAGES",
    "EXTENSION_TO_LANGUAGE",
    "LANGUAGES",
    "Language",
    "language_by_name",
    "language_for_path",
]
