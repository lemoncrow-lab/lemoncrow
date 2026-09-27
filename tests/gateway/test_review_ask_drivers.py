from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lemoncrow.gateway.tools import surface
from lemoncrow.pro.capabilities.review import ask


def test_review_mcp_profile_exposes_review_safe_toolkit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_TOOL_PROFILE", "review")
    assert surface.mcp_tool_profile() == "review"
    for name in ("bash", "code_search", "grep", "read", "web_fetch"):
        assert surface.tool_profile_exposes(name)
        assert surface.tool_visible_to_llm(name)
    assert not surface.tool_profile_exposes("edit")
    assert not surface.tool_profile_exposes("tool")


def test_answer_ask_redacts_server_snapshot_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project_root = tmp_path / "private" / "review-snapshot"
    project_root.mkdir(parents=True)

    def fake_answer(**_kwargs):
        return {
            "intent": "question",
            "body": (f"[README.md:3]({project_root}/README.md:3) changed; " f"also checked {project_root}/src/app.py."),
            "handoff_instruction": "",
        }

    monkeypatch.setattr(ask, "_agent_cli_answer", fake_answer)
    result = ask.answer_ask(
        {"question": "what changed?"},
        backend="codex",
        project_root=project_root,
    )

    assert str(project_root) not in result["body"]
    assert result["body"] == "README.md:3 changed; also checked ./src/app.py."


@pytest.mark.parametrize(
    ("driver", "model", "expected_flags"),
    [
        ("claude", "sonnet", ("--allowedTools", "--json-schema", "--model", "sonnet")),
        ("codex", "gpt-test", ("-a", "never", "-s", "read-only", "-m", "gpt-test")),
    ],
)
def test_agent_cli_ask_drivers_are_read_only_and_parse_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    driver: str,
    model: str,
    expected_flags: tuple[str, ...],
) -> None:
    commands: list[tuple[list[str], Path]] = []
    claude_mcp: list[dict[str, object]] = []
    monkeypatch.setattr(ask.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(command, *, cwd, env=None, capture_output, text, check, timeout):
        commands.append((list(command), Path(cwd)))
        if driver == "claude":
            config_path = Path(command[command.index("--mcp-config") + 1])
            claude_mcp.append(json.loads(config_path.read_text()))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "intent": "question",
                    "body": "Grounded answer",
                    "handoff_instruction": "",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(ask.subprocess, "run", fake_run)

    project_root = tmp_path / "snapshot"
    project_root.mkdir()
    result = ask._agent_cli_answer(
        driver=driver,  # type: ignore[arg-type]
        model=model,
        system="system",
        prompt='{"question":"why"}',
        project_root=project_root,
    )

    assert result["body"] == "Grounded answer"
    command, cwd = commands[0]
    assert cwd == project_root.resolve()
    for flag in expected_flags:
        assert flag in command
    if driver == "claude":
        assert "--strict-mcp-config" in command
        assert "-p" in command
        server = claude_mcp[0]["mcpServers"]["lemoncrow"]  # type: ignore[index]
        assert server["command"] == ask.sys.executable  # type: ignore[index]
        assert server["args"][-2:] == ["--host", "claude"]  # type: ignore[index]
        server_env = server["env"]  # type: ignore[index]
        assert server_env["LEMONCROW_WORKSPACE_ROOT"] == str(project_root.resolve())
        assert server_env["CLAUDE_WORKSPACE_ROOT"] == str(project_root.resolve())
        assert server_env["CURSOR_WORKSPACE_ROOT"] == str(project_root.resolve())
        assert server_env["VSCODE_CWD"] == str(project_root.resolve())
        assert server_env["LEMONCROW_MCP_TOOL_PROFILE"] == "review"
        assert server_env["LEMONCROW_CODE_AUTOSYNC"] == "0"
        assert server_env["LEMONCROW_SERVICE_URL"] == ""
        assert server_env["LEMONCROW_REVIEW_SNAPSHOT_ROOT"] == str(project_root.resolve())
        assert server_env["LEMONCROW_BASH_WORKSPACE_ROOT"].endswith("/execution")
        assert server_env["LEMONCROW_BASH_WORKSPACE_ROOT"] != str(project_root.resolve())
    else:
        assert "exec" in command
        assert "--skip-git-repo-check" in command
        rendered = " ".join(command)
        assert "mcp_servers.lemoncrow.command" in rendered
        assert "mcp_servers.lemoncrow.args" in rendered
        assert "LEMONCROW_WORKSPACE_ROOT" in rendered
        assert "CLAUDE_WORKSPACE_ROOT" in rendered
        assert 'LEMONCROW_MCP_TOOL_PROFILE="review"' in rendered
        assert 'LEMONCROW_CODE_AUTOSYNC="0"' in rendered
        assert 'LEMONCROW_SERVICE_URL=""' in rendered
        assert "LEMONCROW_REVIEW_SNAPSHOT_ROOT" in rendered
        assert "LEMONCROW_BASH_WORKSPACE_ROOT" in rendered
        assert 'mcp_servers.lemoncrow.default_tools_approval_mode="approve"' in rendered
        assert 'mcp_servers.lemoncrow.enabled_tools=["code_search","read","grep","bash","web_fetch"]' in rendered


def test_opencode_review_ask_uses_ollama_and_lemoncrow_mcp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from lemoncrow.gateway.cli import lemoncode_host

    host = tmp_path / "lemoncode-host"
    host.write_text("host")
    monkeypatch.setattr(lemoncode_host, "resolve_host_binary", lambda _root: str(host))

    class Config:
        def get(self, provider: str, key: str) -> str | None:
            return "http://127.0.0.1:11434" if (provider, key) == ("ollama", "base_url") else None

    monkeypatch.setattr("lemoncrow.core.capabilities.providers.config.load_providers_config", lambda _root: Config())
    captured: dict[str, object] = {}

    def fake_run(command, *, cwd, env=None, capture_output, text, check, timeout):
        captured["command"] = list(command)
        captured["cwd"] = Path(cwd)
        captured["env"] = dict(env or {})
        answer = json.dumps({"intent": "question", "body": "Grounded answer", "handoff_instruction": ""})
        event = {"type": "text", "part": {"text": answer}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(event) + "\n", stderr="")

    monkeypatch.setattr(ask.subprocess, "run", fake_run)

    project_root = tmp_path / "snapshot"
    project_root.mkdir()
    result = ask._agent_cli_answer(
        driver="opencode",
        model="ollama/qwen3.8:27b",
        system="system",
        prompt='{"question":"why"}',
        project_root=project_root,
    )

    assert result["body"] == "Grounded answer"
    command = captured["command"]
    assert isinstance(command, list)
    assert command[0] == str(host)
    assert "--pure" in command
    assert "ollama/qwen3.8:27b" in command
    assert "--format" in command and "json" in command
    env = captured["env"]
    assert isinstance(env, dict)
    config = json.loads(str(env["OPENCODE_CONFIG_CONTENT"]))
    assert config["model"] == "ollama/qwen3.8:27b"
    assert config["provider"]["ollama"]["options"]["baseURL"] == "http://127.0.0.1:11434/v1"
    mcp = config["mcp"]["lemoncrow"]
    assert mcp["command"][-2:] == ["--host", "opencode"]
    assert mcp["environment"]["LEMONCROW_MCP_TOOL_PROFILE"] == "review"
    assert mcp["environment"]["LEMONCROW_WORKSPACE_ROOT"] == str(project_root.resolve())


def test_opencode_review_ask_uses_native_model_catalog_and_existing_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from lemoncrow.gateway.cli import lemoncode_host

    host = tmp_path / "lemoncode-host"
    host.write_text("host")
    monkeypatch.setattr(lemoncode_host, "resolve_host_binary", lambda _root: str(host))

    xdg_data = tmp_path / "xdg-data"
    auth_source = xdg_data / "opencode" / "auth.json"
    auth_source.parent.mkdir(parents=True)
    auth_source.write_text('{"opencode":{"key":"secret"}}')
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))

    captured: dict[str, object] = {}

    def fake_run(command, *, cwd, env=None, capture_output, text, check, timeout):
        captured["command"] = list(command)
        captured["cwd"] = Path(cwd)
        captured["env"] = dict(env or {})
        copied_auth = Path(str((env or {})["XDG_DATA_HOME"])) / "opencode" / "auth.json"
        captured["copied_auth"] = copied_auth.read_text()
        answer = json.dumps({"intent": "question", "body": "Grounded answer", "handoff_instruction": ""})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"type": "text", "part": {"text": answer}}) + "\n",
            stderr="",
        )

    monkeypatch.setattr(ask.subprocess, "run", fake_run)

    project_root = tmp_path / "snapshot-native"
    project_root.mkdir()
    result = ask._agent_cli_answer(
        driver="opencode",
        model="opencode/claude-haiku-4-5",
        system="system",
        prompt='{"question":"why"}',
        project_root=project_root,
    )

    assert result["body"] == "Grounded answer"
    command = captured["command"]
    assert isinstance(command, list)
    assert "opencode/claude-haiku-4-5" in command
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["LEMONCODE_CLI_NAME"] == "opencode"
    assert env["LEMONCODE_PRODUCT_NAME"] == "OpenCode"
    assert env["LEMONCODE_STORAGE_NAME"] == "opencode"
    config = json.loads(str(env["OPENCODE_CONFIG_CONTENT"]))
    assert config["model"] == "opencode/claude-haiku-4-5"
    assert config["enabled_providers"] == ["opencode"]
    assert "provider" not in config
    assert captured["copied_auth"] == auth_source.read_text()
