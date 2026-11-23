from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from lemoncrow.gateway.cli import cli


def test_status_reports_products_and_runtime_integrations(tmp_path: Path) -> None:
    review = tmp_path / "review" / "client" / "bin" / "lcr"
    review.parent.mkdir(parents=True)
    review.touch()
    with (
        patch("lemoncrow.gateway.cli.commands.product_status._product_config") as config,
        patch("lemoncrow.gateway.cli.commands.product_status._runtime_integrations") as integrations,
    ):
        config.side_effect = lambda product: {
            "deployment": "hosted" if product == "review" else "local",
            "endpoint": "https://api.lemoncrow.com" if product == "review" else "http://127.0.0.1:7420",
            "authenticated": product == "review",
        }
        integrations.return_value = [
            {"host": "claude", "installed": True, "enabled": True},
            {"host": "codex", "installed": True, "enabled": False},
        ]
        result = CliRunner().invoke(cli, ["--root", str(tmp_path), "status"])

    assert result.exit_code == 0, result.output
    lines = [line.strip() for line in result.output.splitlines()]
    assert lines[0].startswith("◆ LemonCrow ")
    assert "Review" in lines
    assert "✓ Installed    hosted deployment" in lines
    assert "✓ Signed in    yes" in lines
    assert "Workspaces · coding agents" in lines
    assert "full CLI" not in result.output
    assert "✓ claude       enabled" in lines
    assert "○ codex        not enabled" in lines
    assert "→ Manage coding agents with lc rt status" in lines


def test_status_points_at_the_next_step_when_signed_out(tmp_path: Path) -> None:
    review = tmp_path / "review" / "client" / "bin" / "lcr"
    review.parent.mkdir(parents=True)
    review.touch()
    with (
        patch(
            "lemoncrow.gateway.cli.commands.product_status._product_config",
            return_value={"deployment": "hosted", "endpoint": "https://api.lemoncrow.com", "authenticated": False},
        ),
        patch("lemoncrow.gateway.cli.commands.product_status._runtime_integrations", return_value=[]),
    ):
        result = CliRunner().invoke(cli, ["--root", str(tmp_path), "status"])

    assert result.exit_code == 0, result.output
    lines = [line.strip() for line in result.output.splitlines()]
    assert "⚠ Signed in    no" in lines
    assert "→ Sign in with lc auth login" in lines


def test_status_json_is_machine_readable(tmp_path: Path) -> None:
    with (
        patch("lemoncrow.gateway.cli.commands.product_status._review_installed", return_value=False),
        patch(
            "lemoncrow.gateway.cli.commands.product_status._product_config",
            return_value={"deployment": "local", "endpoint": "http://127.0.0.1:7420", "authenticated": False},
        ),
        patch("lemoncrow.gateway.cli.commands.product_status._runtime_integrations", return_value=[]),
    ):
        result = CliRunner().invoke(cli, ["--root", str(tmp_path), "status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["review"]["installed"] is False
    assert payload["runtime"]["installed"] is True
    assert payload["runtime"]["full_cli"] is True
