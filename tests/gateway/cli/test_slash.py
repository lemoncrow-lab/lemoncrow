"""Tests for slash-command parsing."""

from __future__ import annotations

from atelier.gateway.cli.slash import parse_input


def test_empty_line() -> None:
    assert parse_input("").kind == "empty"
    assert parse_input("   ").kind == "empty"


def test_plain_message() -> None:
    parsed = parse_input("hello world")
    assert parsed.kind == "message"
    assert parsed.text == "hello world"


def test_slash_help() -> None:
    parsed = parse_input("/help")
    assert parsed.kind == "slash"
    assert parsed.name == "help"


def test_exit_and_quit() -> None:
    assert parse_input("/exit").kind == "exit"
    assert parse_input("/quit").kind == "exit"


def test_clear() -> None:
    assert parse_input("/clear").kind == "clear"


def test_memory_with_args() -> None:
    parsed = parse_input("/memory foo bar")
    assert parsed.kind == "slash"
    assert parsed.name == "memory"
    assert parsed.args == ["foo", "bar"]


def test_background_with_arg() -> None:
    parsed = parse_input("/background status")
    assert parsed.kind == "slash"
    assert parsed.name == "background"
    assert parsed.args == ["status"]


def test_unknown_command() -> None:
    parsed = parse_input("/unknown")
    assert parsed.kind == "slash"
    assert parsed.name == "unknown"
