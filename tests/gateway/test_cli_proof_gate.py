"""Tests for CLI proof commands after proof reporting moved off MCP."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".lemoncrow"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    return root


def test_cli_proof_run_json_output(cli_env: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(cli_env),
            "proof",
            "run",
            "--session-id",
            "cli-test-run",
            "--context-reduction-pct",
            "55.0",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["session_id"] == "cli-test-run"
    assert data["status"] in ("pass", "fail")
    assert "host_enforcement_matrix" in data


def test_cli_proof_run_text_output(cli_env: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(cli_env),
            "proof",
            "run",
            "--session-id",
            "cli-text-run",
            "--context-reduction-pct",
            "55.0",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "session_id=cli-text-run" in result.output
    assert "status=" in result.output


def test_cli_proof_run_writes_report_json(cli_env: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(cli_env),
            "proof",
            "run",
            "--session-id",
            "save-run",
            "--context-reduction-pct",
            "55.0",
        ],
    )
    assert result.exit_code == 0, result.output
    json_path = cli_env / "proof" / "proof-report.json"
    assert json_path.exists()
    assert json_path.stat().st_size > 0


@pytest.mark.parametrize("command", ["report", "show"])
def test_cli_proof_show_aliases_return_last_run(cli_env: Path, command: str) -> None:
    runner = CliRunner()
    run = runner.invoke(
        cli,
        [
            "--root",
            str(cli_env),
            "proof",
            "run",
            "--session-id",
            f"{command}-run",
            "--context-reduction-pct",
            "55.0",
        ],
    )
    assert run.exit_code == 0, run.output

    result = runner.invoke(cli, ["--root", str(cli_env), "proof", command, "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["session_id"] == f"{command}-run"


def test_cli_proof_report_error_when_no_report(cli_env: Path) -> None:
    result = CliRunner().invoke(cli, ["--root", str(cli_env), "proof", "report"])
    assert result.exit_code != 0
    assert "No proof report found" in result.output


def test_cli_proof_run_fails_low_context_reduction(cli_env: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(cli_env),
            "proof",
            "run",
            "--session-id",
            "low-ctx",
            "--context-reduction-pct",
            "30.0",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "fail"
    assert "context_reduction_pct" in data["failed_thresholds"]


def test_cli_proof_run_passes_with_sufficient_context_reduction(cli_env: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(cli_env),
            "proof",
            "run",
            "--session-id",
            "high-ctx",
            "--context-reduction-pct",
            "60.0",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "context_reduction_pct" not in data.get("failed_thresholds", [])
