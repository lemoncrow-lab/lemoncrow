from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from lemoncrow_client import host_trial

from lemoncrow.gateway.cli import cli


def _which(name: str) -> str | None:
    return f"/opt/bin/{name}" if name in {"claude", "codex", "opencode", "lc"} else None


def test_claude_trial_uses_ephemeral_mcp_and_preserves_host_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_trial.shutil, "which", _which)
    monkeypatch.setattr(host_trial.sys, "argv", ["python"])
    command, env = host_trial.build_trial_launch("claude", ["-p", "hello", "--model", "opus"])

    assert command[0] == "/opt/bin/claude"
    assert command[1] == "--mcp-config"
    config = json.loads(command[2])
    server = config["mcpServers"]["lc"]
    assert server["command"] == "/opt/bin/lc"
    assert server["args"] == ["runtime", "lite-mcp", "--host", "claude"]
    assert server["env"]["LEMONCROW_RUNTIME_LITE"] == "1"
    assert command[3:] == ["-p", "hello", "--model", "opus"]
    assert "--strict-mcp-config" not in command
    assert env.get("ANTHROPIC_BASE_URL") == host_trial.os.environ.get("ANTHROPIC_BASE_URL")


def test_codex_trial_uses_cli_config_overrides_and_preserves_host_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_trial.shutil, "which", _which)
    monkeypatch.setattr(host_trial.sys, "argv", ["python"])
    command, _env = host_trial.build_trial_launch("codex", ["--model", "gpt-5", "fix this"])

    assert command[0] == "/opt/bin/codex"
    rendered = " ".join(command)
    assert 'mcp_servers.lc.command="/opt/bin/lc"' in rendered
    assert 'mcp_servers.lc.args=["runtime","lite-mcp","--host","codex"]' in rendered
    assert 'LEMONCROW_RUNTIME_LITE="1"' in rendered
    assert command[-3:] == ["--model", "gpt-5", "fix this"]


def test_opencode_trial_adds_mcp_overlay_without_dropping_existing_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_trial.shutil, "which", _which)
    monkeypatch.setattr(host_trial.sys, "argv", ["python"])
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"theme":"system","mcp":{"other":{"type":"remote","url":"x"}}}')
    command, env = host_trial.build_trial_launch("opencode", ["run", "hello"])

    assert command == ["/opt/bin/opencode", "run", "hello"]
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["theme"] == "system"
    assert "other" in config["mcp"]
    assert config["mcp"]["lc"]["command"] == ["/opt/bin/lc", "runtime", "lite-mcp", "--host", "opencode"]


def test_trial_command_executes_foreground_child_without_host_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_client.lite import index as lite_index

    monkeypatch.setattr(lite_index, "prepare_lite_workspace", lambda _root: {"ok": True})
    monkeypatch.setattr(host_trial.shutil, "which", _which)
    monkeypatch.setattr(host_trial.sys, "argv", ["python"])
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(command, 0, stdout=str(Path.cwd()) + "\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(host_trial.subprocess, "run", fake_run)
    result = CliRunner().invoke(cli, ["with", "claude", "-p", "hello"])
    assert result.exit_code == 0, result.output
    assert calls and calls[-1][0] == "/opt/bin/claude"
    assert "mcp" not in calls[-1][:1]


def test_trial_command_missing_host_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_client.lite import index as lite_index

    monkeypatch.setattr(lite_index, "prepare_lite_workspace", lambda _root: {"ok": True})
    monkeypatch.setattr(host_trial.shutil, "which", lambda _name: None)
    result = CliRunner().invoke(cli, ["with", "codex"])
    assert result.exit_code == 1
    assert "Codex is not installed or not on PATH" in result.output


def test_trial_mcp_uses_git_root_and_lite_only_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.chdir(nested)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(command, 0, stdout=str(repo) + "\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(host_trial.subprocess, "run", fake_run)
    monkeypatch.setattr(host_trial.shutil, "which", _which)
    command, _env = host_trial.build_trial_launch(
        "claude",
        [],
        mcp_extra_env={"LEMONCROW_LITE_PREPARED": "1"},
    )
    config = json.loads(command[command.index("--mcp-config") + 1])
    mcp_env = config["mcpServers"]["lc"]["env"]
    assert mcp_env["LEMONCROW_WORKSPACE_ROOT"] == str(repo.resolve())
    assert mcp_env["LEMONCROW_RUNTIME_LITE"] == "1"
    assert mcp_env["LEMONCROW_LITE_PREPARED"] == "1"
    assert "LEMONCROW_CODE_AUTOSYNC" not in mcp_env
    assert "LEMONCROW_AUTOSYNC" not in mcp_env


def test_trial_prepares_lite_index_before_host_launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow_client.lite import index as lite_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(host_trial.shutil, "which", _which)
    monkeypatch.setattr(host_trial.sys, "argv", ["python"])
    events: list[tuple[str, object]] = []

    def fake_prepare(root: Path) -> dict[str, object]:
        events.append(("prepare", root))
        return {"ok": True}

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(command, 0, stdout=str(repo) + "\n", stderr="")
        events.append(("launch", command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(lite_index, "prepare_lite_workspace", fake_prepare)
    monkeypatch.setattr(host_trial.subprocess, "run", fake_run)

    assert host_trial.run_trial_host("codex", ["hello"]) == 0
    assert events[0] == ("prepare", repo.resolve())
    assert events[1][0] == "launch"
    assert isinstance(events[1][1], list)
    assert events[1][1][0] == "/opt/bin/codex"


def test_trial_requires_git_workspace_for_code_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_trial.shutil, "which", _which)
    monkeypatch.setattr(host_trial.sys, "argv", ["python"])
    monkeypatch.setattr(
        host_trial.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, stdout="", stderr="not a repo"),
    )
    with pytest.raises(host_trial.HostTrialError, match="indexed code_search is available"):
        host_trial.build_trial_launch("claude", [])


def test_pi_trial_uses_managed_public_host_and_runtime_lite_extension(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setattr(host_trial.sys, "argv", ["python"])
    monkeypatch.setattr(host_trial.shutil, "which", _which)

    command, env = host_trial.build_trial_launch(
        "pi",
        ["-p", "hello"],
        binary_override="/opt/bin/pi-host",
        mcp_extra_env={"LEMONCROW_WORKSPACE_ROOT": str(repo)},
    )

    assert command[0] == "/opt/bin/pi-host"
    assert command[1] == "-e"
    assert command[2].endswith("lemoncrow_client/assets/pi_lite.mjs")
    assert command[3:] == ["-p", "hello"]
    assert "--no-tools" not in command
    assert "--no-context-files" not in command
    assert "--no-skills" not in command
    assert env["LEMONCROW_RUNTIME_LITE"] == "1"
    assert env["LEMONCROW_WORKSPACE_ROOT"] == str(repo)
    assert env["LEMONCROW_PI_LITE_COMMAND"] == "/opt/bin/lc"
    assert json.loads(env["LEMONCROW_PI_LITE_ARGS"]) == ["runtime", "lite-mcp", "--host", "pi"]


@pytest.mark.parametrize("disabled", [False, True])
def test_pi_trial_reuses_native_registration_without_duplicate_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disabled: bool
) -> None:
    from lemoncrow_client import pi_integration

    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setattr(host_trial, "_runtime_mcp_invocation", lambda _host: ("/opt/bin/lc", ["runtime", "lite-mcp"]))
    pi_integration.install()
    arguments = ["--no-extensions", "-p", "hello"] if disabled else ["-p", "hello"]
    command, _env = host_trial.build_trial_launch(
        "pi",
        arguments,
        binary_override="/opt/bin/pi-host",
        mcp_extra_env={"LEMONCROW_WORKSPACE_ROOT": str(tmp_path)},
    )
    assert command.count("-e") == int(disabled)
    assert command[-len(arguments) :] == arguments


def test_pi_trial_missing_managed_host_is_actionable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow_client import pi_host

    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(pi_host, "resolve_host_binary", lambda _root: None)
    with pytest.raises(host_trial.HostTrialError, match="lc rt host install --engine pi"):
        host_trial.build_trial_launch("pi", [])
