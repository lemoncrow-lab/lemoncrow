from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from atelier.gateway.cli import cli


def _run_uninstall_script(
    repo_root: Path,
    *,
    home: Path,
    install_dir: Path,
    protected_roots: str | None = None,
) -> subprocess.CompletedProcess[str]:
    (home / ".atelier").mkdir(parents=True)
    (home / ".atelier" / "install_dir").write_text(str(install_dir), encoding="utf-8")
    install_dir.mkdir(parents=True)
    (install_dir / "sentinel.txt").write_text("keep", encoding="utf-8")

    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "SHELL": "/bin/bash",
        "ATELIER_BIN_DIR": str(home / ".local" / "bin"),
        "ATELIER_TOOL_DIR": str(home / ".local" / "share" / "uv" / "tools"),
    }
    if protected_roots is not None:
        env["ATELIER_PROTECTED_SOURCE_ROOTS"] = protected_roots

    return subprocess.run(
        ["bash", str(repo_root / "scripts" / "uninstall.sh"), "--purge", "--no-hosts"],
        cwd=str(repo_root),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_uninstall_purge_preserves_install_dir_under_projects(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    install_dir = home / "Projects" / "atelier"

    result = _run_uninstall_script(repo_root, home=home, install_dir=install_dir)

    assert result.returncode == 0, result.stderr
    assert install_dir.exists()
    assert (install_dir / "sentinel.txt").exists()
    assert "Skipping install source under protected source root" in result.stdout


def test_uninstall_purge_removes_default_managed_install_dir(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    install_dir = home / ".local" / "share" / "atelier"

    result = _run_uninstall_script(repo_root, home=home, install_dir=install_dir)

    assert result.returncode == 0, result.stderr
    assert not install_dir.exists()
    assert "Removed" in result.stdout


def test_uninstall_command_calls_script(tmp_path: Path) -> None:
    runner = CliRunner()
    # Mock _project_root to point to a temp dir where we'll place a dummy script
    with patch("atelier.gateway.cli.commands.admin._project_root") as mock_root:
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
    with patch("atelier.gateway.cli.commands.admin._project_root") as mock_root:
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
    with patch("atelier.gateway.cli.commands.admin._project_root") as mock_root:
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
    with patch("atelier.gateway.cli.commands.admin._project_root") as mock_root:
        mock_root.return_value = tmp_path
        # scripts/uninstall.sh does not exist
        result = runner.invoke(cli, ["uninstall"])
        assert result.exit_code != 0
        assert "uninstall script not found" in result.output


def test_uninstall_command_failure(tmp_path: Path) -> None:
    runner = CliRunner()
    with patch("atelier.gateway.cli.commands.admin._project_root") as mock_root:
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


def _run_host_uninstall(
    repo_root: Path, script_name: str, home: Path, workspace: Path
) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "SHELL": "/bin/bash",
    }
    return subprocess.run(
        ["bash", str(repo_root / "scripts" / script_name), "--workspace", str(workspace)],
        cwd=str(repo_root),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_uninstall_codex_cleans_workspace_marketplace_and_agents(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    marketplace = workspace / ".agents" / "plugins" / "marketplace.json"
    agents_dir = workspace / ".codex" / "agents"
    plugin_dir = workspace / ".codex" / "plugins" / "atelier"
    tasks_dir = workspace / ".codex" / "tasks"
    marketplace.parent.mkdir(parents=True)
    agents_dir.mkdir(parents=True)
    plugin_dir.mkdir(parents=True)
    tasks_dir.mkdir(parents=True)
    marketplace.write_text(json.dumps({"plugins": [{"name": "other"}, {"name": "atelier"}]}) + "\n", encoding="utf-8")
    (agents_dir / "atelier.code.toml").write_text("name = 'atelier.code'\n", encoding="utf-8")
    (agents_dir / "custom.toml").write_text("name = 'custom'\n", encoding="utf-8")
    (plugin_dir / "plugin.json").write_text("{}\n", encoding="utf-8")
    (tasks_dir / "preflight.md").write_text("task\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text(
        "before\n<!-- ATELIER START -->\natelier:code\n<!-- ATELIER END -->\nafter\n", encoding="utf-8"
    )

    result = _run_host_uninstall(repo_root, "uninstall_codex.sh", home, workspace)

    assert result.returncode == 0, result.stderr
    assert json.loads(marketplace.read_text(encoding="utf-8"))["plugins"] == [{"name": "other"}]
    assert not (agents_dir / "atelier.code.toml").exists()
    assert (agents_dir / "custom.toml").exists()
    assert not plugin_dir.exists()
    assert not tasks_dir.exists()
    assert "atelier:code" not in (workspace / "AGENTS.md").read_text(encoding="utf-8")


def test_uninstall_opencode_cleans_provider_permissions_plugins_and_agents(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    config = workspace / "opencode.json"
    agents_dir = workspace / ".opencode" / "agents"
    plugins_dir = workspace / ".opencode" / "plugins"
    staging_dir = home / ".atelier" / "opencode"
    agents_dir.mkdir(parents=True)
    plugins_dir.mkdir(parents=True)
    staging_dir.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "default_agent": "atelier",
                "mcp": {"atelier": {}, "other": {}},
                "provider": {"atelier": {}, "other": {}},
                "permission": {"atelier_*": "allow", "other_*": "ask"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (agents_dir / "atelier.code.md").write_text("agent\n", encoding="utf-8")
    (agents_dir / "custom.md").write_text("agent\n", encoding="utf-8")
    (plugins_dir / "atelier-nudge.js").write_text("plugin\n", encoding="utf-8")
    (plugins_dir / "atelier_nudge.py").write_text("plugin\n", encoding="utf-8")

    result = _run_host_uninstall(repo_root, "uninstall_opencode.sh", home, workspace)

    assert result.returncode == 0, result.stderr
    data = json.loads(config.read_text(encoding="utf-8"))
    assert "default_agent" not in data
    assert data["mcp"] == {"other": {}}
    assert data["provider"] == {"other": {}}
    assert data["permission"] == {"other_*": "ask"}
    assert not (agents_dir / "atelier.code.md").exists()
    assert (agents_dir / "custom.md").exists()
    assert not (plugins_dir / "atelier-nudge.js").exists()
    assert not (plugins_dir / "atelier_nudge.py").exists()
    assert not staging_dir.exists()


def test_uninstall_claude_cleans_workspace_settings_agents_and_skills(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    claude_dir = workspace / ".claude"
    agents_dir = claude_dir / "agents"
    skills_dir = claude_dir / "skills" / "example"
    agents_dir.mkdir(parents=True)
    skills_dir.mkdir(parents=True)
    (agents_dir / "atelier.code.md").write_text("agent\n", encoding="utf-8")
    (agents_dir / "custom.md").write_text("agent\n", encoding="utf-8")
    (skills_dir / "SKILL.md").write_text("skill\n", encoding="utf-8")
    (claude_dir / "settings.local.json").write_text(
        json.dumps({"env": {"CLAUDE_WORKSPACE_ROOT": str(workspace), "KEEP": "1"}, "agent": "atelier:code"}) + "\n",
        encoding="utf-8",
    )
    (claude_dir / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": ["mcp__atelier__read", "Bash(git *)", "Other"],
                    "deny": ["Read", "OtherDeny"],
                },
                "statusLine": {"command": "atelier status"},
                "subagentStatusLine": {"command": "atelier status"},
                "agent": "atelier:code",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_host_uninstall(repo_root, "uninstall_claude.sh", home, workspace)

    assert result.returncode == 0, result.stderr
    local_settings = json.loads((claude_dir / "settings.local.json").read_text(encoding="utf-8"))
    assert local_settings == {"env": {"KEEP": "1"}}
    settings = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    assert settings == {"permissions": {"allow": ["Other"], "deny": ["OtherDeny"]}}
    assert not (agents_dir / "atelier.code.md").exists()
    assert (agents_dir / "custom.md").exists()
    assert not (claude_dir / "skills").exists()
