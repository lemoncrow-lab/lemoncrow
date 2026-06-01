"""Tests for the benchmark CLI subcommand workflow."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from atelier.gateway.cli import cli


def test_benchmark_legacy_top_level_commands_are_removed(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"

    assert runner.invoke(cli, ["--root", str(root), "benchmark-core", "--json"]).exit_code != 0
    assert runner.invoke(cli, ["--root", str(root), "benchmark", "--prompt", "Fix PDP", "--json"]).exit_code != 0


def test_help_command_shows_root_command_help(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"

    root_help = runner.invoke(cli, ["--root", str(root), "help"])
    assert root_help.exit_code == 0, root_help.output
    assert "Commands:" in root_help.output
    assert "benchmark" in root_help.output
