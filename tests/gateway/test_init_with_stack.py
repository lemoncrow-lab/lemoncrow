from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from atelier.gateway.cli import cli


def test_init_with_stack_copies_templates(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    result = CliRunner().invoke(cli, ["--root", str(root), "init", "--stack", "python-fastapi"])

    assert result.exit_code == 0, result.output
    # Lessons are project-local by default in .lessons/
    blocks_dir = tmp_path / ".lessons" / "blocks"
    copied = sorted(blocks_dir.glob("template_*.md"))
    assert len(copied) == 8
    assert any(path.name == "template_pydantic-api-boundaries.md" for path in copied)


def test_init_list_stacks() -> None:
    result = CliRunner().invoke(cli, ["init", "--list-stacks"])

    assert result.exit_code == 0, result.output
    assert "python-fastapi" in result.output
