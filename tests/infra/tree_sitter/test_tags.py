from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.infra.tree_sitter.tags import Tag, extract_tags_from_text


def _definitions(tags: list[Tag]) -> set[str]:
    return {tag.name for tag in tags if tag.kind == "definition"}


@pytest.mark.parametrize(
    ("path", "source", "expected"),
    [
        (
            "Store.java",
            "package app;\nclass Store { int total; void checkout() {} }\n",
            {"Store", "total", "checkout"},
        ),
        (
            "invoice.rb",
            "module Billing\n  class Invoice\n    def total\n      1\n    end\n  end\nend\n",
            {"Billing", "Invoice", "total"},
        ),
        ("store.cs", "namespace App { class Store { public void Add() {} } }\n", {"Store", "Add"}),
        ("store.kt", "class Store { fun add(): Unit {} }\nfun top() {}\n", {"Store", "add", "top"}),
        (
            "store.php",
            "<?php\nclass Store { public function add() {} }\nfunction top() {}\n",
            {"Store", "add", "top"},
        ),
        ("store.swift", "struct Store { func add() {} }\nfunc top() {}\n", {"Store", "add", "top"}),
        (
            "store.scala",
            "class Store { def add(): Unit = {} }\ndef top(): Unit = {}\n",
            {"Store", "add", "top"},
        ),
        (
            "store.c",
            "typedef struct Order { int id; } Order;\nint total(int x) { return x; }\n",
            {"Order", "total"},
        ),
        (
            "store.cpp",
            "namespace app { class Store { public: void add() {} }; }\n",
            {"app", "Store", "add"},
        ),
    ],
)
def test_tree_sitter_definitions_for_previously_regex_unsupported_languages(
    path: str, source: str, expected: set[str]
) -> None:
    tags = extract_tags_from_text(source, Path(path))

    assert expected <= _definitions(tags)


@pytest.mark.parametrize(
    ("path", "source", "expected"),
    [
        (
            "deploy.sh",
            'NAME=value\nfunction deploy() { echo "$NAME"; }\ncleanup() { echo done; }\n',
            {"NAME", "deploy", "cleanup"},
        ),
        (
            "schema.sql",
            "CREATE TABLE users (id INT);\nCREATE INDEX idx_users_id ON users(id);\n",
            {"users", "idx_users_id"},
        ),
        (
            "config.toml",
            '[package]\nname = "demo"\n[tool.demo]\nversion = "1"\n',
            {"package", "name", "tool.demo", "version"},
        ),
        ("service.yaml", "name: demo\nservices:\n  api:\n    image: app\n", {"name", "services"}),
        ("package.json", '{"name":"demo","scripts":{"test":"pytest"}}\n', {"name", "scripts"}),
    ],
)
def test_tree_sitter_definitions_for_shell_sql_and_data_languages(path: str, source: str, expected: set[str]) -> None:
    tags = extract_tags_from_text(source, Path(path))

    assert expected <= _definitions(tags)


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("config.toml", '[package]\nname = "demo"\n'),
        ("service.yaml", "name: demo\nservices:\n  api:\n    image: app\n"),
        ("package.json", '{"name":"demo","scripts":{"test":"pytest"}}\n'),
    ],
)
def test_data_language_tags_are_definition_only(path: str, source: str) -> None:
    tags = extract_tags_from_text(source, Path(path))

    assert {tag.kind for tag in tags} == {"definition"}


def test_json_tags_are_bounded_to_top_level_keys() -> None:
    tags = extract_tags_from_text('{"name":"demo","scripts":{"test":"pytest"}}\n', Path("package.json"))

    assert "test" not in _definitions(tags)


def test_data_language_parser_missing_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_parser(language: str) -> None:
        assert language == "json"
        return None

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.semantic_file_memory.treesitter_ast.tree_sitter_parser",
        missing_parser,
    )

    assert extract_tags_from_text('{"name":"demo"}\n', Path("package.json")) == []


def test_tag_byte_range_points_to_name() -> None:
    source = '{"name":"demo"}\n'
    tags = extract_tags_from_text(source, Path("package.json"))
    tag = next(tag for tag in tags if tag.name == "name")
    start, end = tag.byte_range

    assert source.encode("utf-8")[start:end].decode("utf-8") == "name"


def test_malformed_data_language_does_not_raise() -> None:
    assert extract_tags_from_text('{"name":', Path("package.json")) == []
