from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability
from atelier.core.capabilities.semantic_file_memory.capability import _outline_saves_enough
from atelier.core.capabilities.semantic_file_memory.treesitter_ast import (
    SUPPORTED_LANGUAGES,
    outline_text,
)
from atelier.infra.code_intel.languages import LANGUAGES, language_for_path
from atelier.infra.tree_sitter.tags import Tag, extract_tags_from_text

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "languages"
AST_OUTLINE_LANGUAGES = {"python", "typescript", "javascript"}


@dataclass(frozen=True)
class LanguageFixture:
    language: str
    filename: str
    expected_definitions: frozenset[str] = frozenset()

    @property
    def path(self) -> Path:
        return FIXTURE_DIR / self.filename


FIXTURES: tuple[LanguageFixture, ...] = (
    LanguageFixture("python", "sample.py", frozenset({"SampleService", "helper"})),
    LanguageFixture("typescript", "sample.ts", frozenset({"Item", "Cart", "checkout"})),
    LanguageFixture("javascript", "sample.js", frozenset({"Cart", "checkout"})),
    LanguageFixture("bash", "sample.sh", frozenset({"APP_NAME", "DEPLOY_ENV", "build_app", "deploy_app"})),
    LanguageFixture("csharp", "Sample.cs", frozenset({"SampleService", "total", "Add"})),
    LanguageFixture("go", "sample.go", frozenset({"SampleService", "Run", "Helper"})),
    LanguageFixture("rust", "sample.rs", frozenset({"SampleService", "run", "helper"})),
    LanguageFixture("java", "Sample.java", frozenset({"Sample", "total", "add"})),
    LanguageFixture("kotlin", "Sample.kt", frozenset({"SampleService", "add", "helper"})),
    LanguageFixture("scala", "Sample.scala", frozenset({"SampleService", "add", "helper"})),
    LanguageFixture("ruby", "sample.rb", frozenset({"Fixtures", "SampleService", "add"})),
    LanguageFixture("cpp", "sample.cpp", frozenset({"fixtures", "SampleService", "add"})),
    LanguageFixture("c", "sample.c", frozenset({"SampleService", "add"})),
    LanguageFixture("swift", "sample.swift", frozenset({"SampleService", "add", "helper"})),
    LanguageFixture("php", "sample.php", frozenset({"Fixtures", "SampleService", "add", "helper"})),
    LanguageFixture("sql", "sample.sql", frozenset({"users", "idx_users_email", "active_users"})),
    LanguageFixture("markdown", "sample.md"),
    LanguageFixture("yaml", "config.yaml", frozenset({"name", "services", "metadata"})),
    LanguageFixture("toml", "config.toml", frozenset({"package", "name", "tool.atelier", "tool.atelier.metadata"})),
    LanguageFixture("json", "config.json", frozenset({"name", "version", "settings", "metadata"})),
    LanguageFixture("html", "sample.html"),
    LanguageFixture("css", "sample.css", frozenset({"button", "header"})),
    LanguageFixture("lua", "sample.lua", frozenset({"M", "helper"})),
    LanguageFixture("make", "Makefile", frozenset({"all", "build", "clean"})),
)


def _definitions(tags: list[Tag]) -> set[str]:
    return {tag.name for tag in tags if tag.kind == "definition"}


def _fixture_ids(fixture: LanguageFixture) -> str:
    return fixture.language


def test_language_fixture_matrix_covers_canonical_registry() -> None:
    assert {fixture.language for fixture in FIXTURES} == {language.name for language in LANGUAGES}
    assert all(fixture.path.is_file() for fixture in FIXTURES)


@pytest.mark.parametrize("fixture", FIXTURES, ids=_fixture_ids)
def test_language_detection_matrix(fixture: LanguageFixture, tmp_path: Path) -> None:
    language = language_for_path(fixture.path)
    assert language is not None
    assert language.name == fixture.language

    cap = SemanticFileMemoryCapability(tmp_path)
    payload = cap.smart_read(fixture.path, outline_threshold=0)

    assert payload["language"] == fixture.language


@pytest.mark.parametrize(
    "fixture",
    [fixture for fixture in FIXTURES if fixture.language in SUPPORTED_LANGUAGES],
    ids=_fixture_ids,
)
def test_tree_sitter_outline_matrix_honors_guard(fixture: LanguageFixture, tmp_path: Path) -> None:
    source = fixture.path.read_text(encoding="utf-8")
    dedicated_outline = outline_text(fixture.language, source)

    assert dedicated_outline

    cap = SemanticFileMemoryCapability(tmp_path)
    payload = cap.smart_read(fixture.path, outline_threshold=0)
    guard_passes = _outline_saves_enough(dedicated_outline, source)

    if fixture.language in AST_OUTLINE_LANGUAGES:
        assert payload["mode"] == "outline"
        assert payload["outline"]["language"] in {fixture.language, "tsx"}
    elif guard_passes:
        assert payload["mode"] == "outline"
        assert payload["outline"]["kind"] == "treesitter"
    else:
        outline = payload.get("outline")
        assert not (isinstance(outline, dict) and outline.get("kind") == "treesitter")


@pytest.mark.parametrize("fixture", FIXTURES, ids=_fixture_ids)
def test_language_definition_tag_matrix(fixture: LanguageFixture) -> None:
    tags = extract_tags_from_text(fixture.path.read_text(encoding="utf-8"), fixture.path)

    if fixture.expected_definitions:
        assert fixture.expected_definitions <= _definitions(tags)
    else:
        assert _definitions(tags) == set()
