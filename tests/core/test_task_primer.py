"""Tests for the owned-session workspace primer."""

from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.owned_agent_session.task_primer import build_task_primer


def _make_workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "parser.rs").write_text("fn parse_expr(input: &str) -> Result<Expr, Error> {\n    todo!()\n}\n")
    (tmp_path / "README.md").write_text("A toy crate.\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return tmp_path


def test_primer_contains_tree_and_keyword_excerpts(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    primer = build_task_primer("Fix the parse_expr error handling", ws)
    assert "src/parser.rs" in primer
    assert "README.md" in primer
    assert "1: fn parse_expr" in primer  # keyword-matched excerpt with line number
    assert ".git" not in primer


def test_primer_empty_workspace_returns_empty(tmp_path: Path) -> None:
    assert build_task_primer("anything", tmp_path) == ""


def test_primer_respects_max_chars(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    primer = build_task_primer("Fix the parse_expr error handling", ws, max_chars=100)
    assert len(primer) <= 100


def test_primer_never_raises_on_bad_workspace(tmp_path: Path) -> None:
    assert build_task_primer("task", tmp_path / "does-not-exist") == ""
