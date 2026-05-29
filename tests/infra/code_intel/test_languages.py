"""Unit tests for the canonical language registry (DLS-LANG-01/02/03/04)."""

from __future__ import annotations

import dataclasses

import pytest

from atelier.core.capabilities.semantic_file_memory.treesitter_ast import _LANG_CONFIG
from atelier.infra.code_intel.languages import (
    ALL_LANGUAGES,
    EXTENSION_TO_LANGUAGE,
    Language,
    language_by_name,
    language_for_path,
)

# ---------------------------------------------------------------------------
# Legacy expectation table — sourced verbatim from
# semantic_file_memory.capability._language_for, EXCEPT the shell extensions
# (.sh/.bash/.zsh) which now canonicalize to "bash" (DLS-LANG-03) instead of
# the legacy "shell" spelling.
# ---------------------------------------------------------------------------
_LEGACY_EXTENSION_TO_NAME: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".sql": "sql",
    ".md": "markdown",
    ".markdown": "markdown",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".rb": "ruby",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".c": "c",
    ".h": "c",
    ".swift": "swift",
    ".php": "php",
    ".sh": "bash",  # was "shell" — canonicalized (DLS-LANG-03)
    ".bash": "bash",  # was "shell"
    ".zsh": "bash",  # was "shell"
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
}


# ---------------------------------------------------------------------------
# DLS-LANG-01: public surface exists with the expected types
# ---------------------------------------------------------------------------


def test_public_surface_types() -> None:
    assert dataclasses.is_dataclass(Language)
    # Frozen dataclass: fields are immutable.
    fields = {f.name for f in dataclasses.fields(Language)}
    assert fields == {"name", "extensions", "parser_name", "scip_indexer"}
    sample = language_by_name("python")
    assert sample is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        sample.name = "mutated"  # type: ignore[misc]


def test_public_surface_callables_and_containers() -> None:
    assert callable(language_for_path)
    assert callable(language_by_name)
    assert isinstance(EXTENSION_TO_LANGUAGE, dict)
    assert isinstance(ALL_LANGUAGES, frozenset)
    assert "bash" in ALL_LANGUAGES


# ---------------------------------------------------------------------------
# DLS-LANG-02 (legacy coverage): every legacy extension resolves to its prior
# canonical name, except shell extensions which now map to "bash".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extension", "expected_name"),
    sorted(_LEGACY_EXTENSION_TO_NAME.items()),
)
def test_legacy_extensions_resolve_to_canonical_name(extension: str, expected_name: str) -> None:
    resolved = language_for_path(f"file{extension}")
    assert resolved is not None, f"extension {extension} dropped from registry"
    assert resolved.name == expected_name


# ---------------------------------------------------------------------------
# DLS-LANG-02 (fallback): unknown / extensionless paths resolve to None.
# ---------------------------------------------------------------------------


def test_unknown_extension_resolves_to_none() -> None:
    assert language_for_path("file.xyz") is None


def test_no_extension_resolves_to_none() -> None:
    assert language_for_path("noext") is None


# ---------------------------------------------------------------------------
# DLS-LANG-03 (boundary): shell extensions canonicalize to "bash".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extension", [".sh", ".bash", ".zsh"])
def test_shell_extensions_canonical_to_bash(extension: str) -> None:
    resolved = language_for_path(f"script{extension}")
    assert resolved is not None
    assert resolved.name == "bash"


def test_csharp_canonical_spelling() -> None:
    resolved = language_for_path("Program.cs")
    assert resolved is not None
    assert resolved.name == "csharp"


# ---------------------------------------------------------------------------
# DLS-LANG-04 (drift guard): every _LANG_CONFIG key must resolve via the
# registry, protecting the SUPPORTED_LANGUAGES↔registry contract (Pitfall 2).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang_key", sorted(_LANG_CONFIG.keys()))
def test_lang_config_keys_present_in_registry(lang_key: str) -> None:
    assert (
        language_by_name(lang_key) is not None
    ), f"_LANG_CONFIG key {lang_key!r} is absent from the canonical registry"
