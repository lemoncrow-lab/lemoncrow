from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from lemoncrow.gateway.cli import cli


def test_optimize_run_blocks_open_pr_when_proposal_not_approved(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("lemoncrow.gateway.cli.commands.savings._load_store", lambda root: object())
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.optimization.run_optimization_cycle",
        lambda **kwargs: {
            "repo_root": str(tmp_path),
            "advisor": {"current_policy": {"preset": "balanced"}, "weekly_savings_usd": 1.0, "confidence": "low"},
            "proposal": {"action": "missing_non_inferiority_evidence", "artifact_path": None, "open_pr": None},
        },
    )

    result = CliRunner().invoke(cli, ["--root", str(tmp_path / ".lemoncrow"), "optimize", "run", "--open-pr"])

    assert result.exit_code != 0
    assert "open-pr blocked: missing_non_inferiority_evidence" in result.output


def test_optimize_auto_commands_persist_canonical_config(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    runner = CliRunner()

    enable = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "optimize",
            "auto",
            "enable",
            "--proposal-tokens-threshold",
            "2500",
            "--json",
        ],
    )
    assert enable.exit_code == 0, enable.output
    assert '"enabled": true' in enable.output.lower()

    status = runner.invoke(cli, ["--root", str(root), "optimize", "auto", "status", "--json"])
    assert status.exit_code == 0, status.output
    assert '"minimum_projected_tokens_saved": 2500' in status.output

    disable = runner.invoke(cli, ["--root", str(root), "optimize", "auto", "disable", "--json"])
    assert disable.exit_code == 0, disable.output
    assert '"enabled": false' in disable.output.lower()
