from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from atelier.gateway.cli import cli


def test_uninstall_command_calls_script(tmp_path: Path) -> None:
    runner = CliRunner()
    # Mock _project_root to point to a temp dir where we'll place a dummy script
    with patch("atelier.gateway.cli.app._project_root") as mock_root:
        mock_root.return_value = tmp_path
        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        uninstall_script = script_dir / "uninstall.sh"
        uninstall_script.touch()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(cli, ["uninstall", "--dry-run", "--no-hosts"])

            assert result.exit_code == 0
            mock_run.assert_called_once()
            args, _ = mock_run.call_args
            cmd = args[0]
            assert "uninstall.sh" in str(cmd[1])
            assert "--dry-run" in cmd
            assert "--no-hosts" in cmd


def test_uninstall_command_with_workspace(tmp_path: Path) -> None:
    runner = CliRunner()
    with patch("atelier.gateway.cli.app._project_root") as mock_root:
        mock_root.return_value = tmp_path
        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        uninstall_script = script_dir / "uninstall.sh"
        uninstall_script.touch()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            workspace_path = tmp_path / "my-workspace"
            result = runner.invoke(cli, ["uninstall", "--workspace", str(workspace_path)])

            assert result.exit_code == 0
            mock_run.assert_called_once()
            args, _ = mock_run.call_args
            cmd = args[0]
            assert "--workspace" in cmd
            assert str(workspace_path) in cmd


def test_uninstall_command_with_purge(tmp_path: Path) -> None:
    runner = CliRunner()
    with patch("atelier.gateway.cli.app._project_root") as mock_root:
        mock_root.return_value = tmp_path
        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        uninstall_script = script_dir / "uninstall.sh"
        uninstall_script.touch()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = runner.invoke(cli, ["uninstall", "--purge", "--dry-run"])

            assert result.exit_code == 0
            mock_run.assert_called_once()
            args, _ = mock_run.call_args
            cmd = args[0]
            assert "--purge" in cmd
            assert "--dry-run" in cmd


def test_uninstall_command_script_not_found(tmp_path: Path) -> None:
    runner = CliRunner()
    with patch("atelier.gateway.cli.app._project_root") as mock_root:
        mock_root.return_value = tmp_path
        # scripts/uninstall.sh does not exist
        result = runner.invoke(cli, ["uninstall"])
        assert result.exit_code != 0
        assert "uninstall script not found" in result.output


def test_uninstall_command_failure(tmp_path: Path) -> None:
    runner = CliRunner()
    with patch("atelier.gateway.cli.app._project_root") as mock_root:
        mock_root.return_value = tmp_path
        script_dir = tmp_path / "scripts"
        script_dir.mkdir()
        uninstall_script = script_dir / "uninstall.sh"
        uninstall_script.touch()

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, ["bash"])
        result = runner.invoke(cli, ["uninstall"])
        assert result.exit_code != 0
        assert "uninstall failed with code 1" in result.output
