from __future__ import annotations

from click.testing import CliRunner

from atelier.gateway.cli import cli


def test_cli_help_shows_core_commands() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output

    output = result.output
    assert "init" in output
    assert "context-report" in output
    assert "proof" in output
    assert "search-read" in output
