"""Canonical language registry — the single source of truth for language identity.

This stdlib-only leaf module unifies the previously duplicated extension→language
maps (``semantic_file_memory.capability._language_for`` and
``tool_supervision.search_read._LANG_MAP``) into one frozen table. ``name`` is the
tree-sitter-language-pack parser name; ``parser_name`` usually equals ``name``.

Design constraints:

* Imports nothing from ``lemoncrow.core`` (avoids the import cycle — Pitfall 1).
* Shell extensions canonicalize to ``bash`` at the data layer (DLS-LANG-03); the
  legacy ``"shell"`` spelling is intentionally not reproduced here.
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
    filenames: frozenset[str] = frozenset()


LANGUAGES: tuple[Language, ...] = (
    Language("python", frozenset({".py", ".pyi"}), "python"),
    Language("typescript", frozenset({".ts", ".tsx"}), "typescript"),
    Language("javascript", frozenset({".js", ".jsx", ".mjs", ".cjs"}), "javascript"),
    # Shell extensions canonicalize to bash (DLS-LANG-03).
    Language("bash", frozenset({".sh", ".bash", ".zsh"}), "bash"),
    # Canonical spelling is `csharp`, NOT `c_sharp`.
    Language("csharp", frozenset({".cs"}), "csharp"),
    Language("go", frozenset({".go"}), "go"),
    Language("rust", frozenset({".rs"}), "rust"),
    Language("java", frozenset({".java"}), "java"),
    Language("kotlin", frozenset({".kt", ".kts"}), "kotlin"),
    Language("scala", frozenset({".scala"}), "scala"),
    Language("ruby", frozenset({".rb"}), "ruby"),
    Language("cpp", frozenset({".cpp", ".cc", ".cxx", ".hpp", ".hh"}), "cpp"),
    Language("c", frozenset({".c", ".h"}), "c"),
    Language("swift", frozenset({".swift"}), "swift"),
    Language("php", frozenset({".php"}), "php"),
    Language("sql", frozenset({".sql"}), "sql"),
    Language("markdown", frozenset({".md", ".markdown"}), "markdown"),
    Language("yaml", frozenset({".yaml", ".yml"}), "yaml"),
    Language("toml", frozenset({".toml"}), "toml"),
    Language("json", frozenset({".json"}), "json"),
    Language("html", frozenset({".html", ".htm"}), "html"),
    Language("css", frozenset({".css"}), "css"),
    Language("lua", frozenset({".lua"}), "lua"),
    Language("make", frozenset(), "make", frozenset({"GNUmakefile", "Makefile", "makefile"})),
)


EXTENSION_TO_LANGUAGE: dict[str, Language] = {
    extension: language for language in LANGUAGES for extension in language.extensions
}
FILENAME_TO_LANGUAGE: dict[str, Language] = {
    filename: language for language in LANGUAGES for filename in language.filenames
}

_BY_NAME: dict[str, Language] = {language.name: language for language in LANGUAGES}

ALL_LANGUAGES: frozenset[str] = frozenset(_BY_NAME)


def language_for_path(path: str | Path) -> Language | None:
    """Resolve a file path to its ``Language`` record, or ``None`` if unknown.

    Callers map ``None`` to ``"text"`` at their own boundary (DLS-LANG-02).
    """

    candidate = Path(path)
    return FILENAME_TO_LANGUAGE.get(candidate.name) or EXTENSION_TO_LANGUAGE.get(candidate.suffix.lower())


def language_by_name(name: str) -> Language | None:
    """Resolve a canonical language name to its ``Language`` record."""

    return _BY_NAME.get(name)


__all__ = [
    "ALL_LANGUAGES",
    "EXTENSION_TO_LANGUAGE",
    "FILENAME_TO_LANGUAGE",
    "LANGUAGES",
    "Language",
    "language_by_name",
    "language_for_path",
]
