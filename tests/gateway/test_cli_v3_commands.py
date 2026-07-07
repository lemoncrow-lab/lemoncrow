from __future__ import annotations

import pytest
from click.testing import CliRunner

from atelier.gateway.cli import cli


def test_cli_letta_commands_route_to_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "atelier.gateway.integrations.openmemory_lifecycle.run_compose",
        lambda args: calls.append(args),
    )
    runner = CliRunner()

    up = runner.invoke(cli, ["letta", "up"])
    down = runner.invoke(cli, ["letta", "down"])
    reset = runner.invoke(cli, ["letta", "reset", "--yes"])

    assert up.exit_code == 0, up.output
    assert down.exit_code == 0, down.output
    assert reset.exit_code == 0, reset.output
    assert calls == [["up", "-d"], ["down"], ["down", "-v"]]


def test_cli_letta_reset_requires_confirmation() -> None:
    result = CliRunner().invoke(cli, ["letta", "reset"])
    assert result.exit_code != 0
    assert "without --yes" in result.output
