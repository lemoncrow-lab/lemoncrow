from click.testing import CliRunner

from lemoncrow.gateway.cli import cli


def test_retired_runtime_commands_are_absent() -> None:
    runner = CliRunner()
    for command in ("stack", "servicectl", "background", "systemd"):
        result = runner.invoke(cli, [command])
        assert result.exit_code == 2, (command, result.output)
        assert "No such command" in result.output


def test_independent_service_and_worker_commands_remain_registered() -> None:
    runner = CliRunner()
    assert runner.invoke(cli, ["service", "--help"]).exit_code == 0
    assert runner.invoke(cli, ["worker", "--help"]).exit_code == 0
