from __future__ import annotations

import json
from pathlib import Path

from lemoncrow.gateway.cli.coding_engine import _build_engine_launch


def _launch(tmp_path: Path, engine: str, *, prompt: str | None = None, resume: str | None = None):
    return _build_engine_launch(
        engine=engine,
        executable=f"/bin/{engine}",
        base_url="http://127.0.0.1:43210",
        token="secret",
        project_root=tmp_path,
        empty_mcp_config=tmp_path / "empty.json",
        budget="balanced",
        prompt=prompt,
        resume=resume,
        base_env={"PATH": "/bin"},
    )


def test_lemoncode_is_a_managed_controlled_frontend(tmp_path: Path) -> None:
    launch = _launch(tmp_path, "lemoncode", prompt="fix it")
    config = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])

    assert launch.command[:2] == ("/bin/lemoncode", "--pure")
    assert launch.command[-2:] == ("run", "fix it")
    assert config["model"] == "lc/lemoncrow"
    assert config["enabled_providers"] == ["lc"]
    assert config["share"] == "disabled"
    assert config["snapshot"] is False
    assert config["autoupdate"] is False
    assert config["provider"]["lc"]["options"]["baseURL"].endswith("/v1")
    assert config["provider"]["lc"]["models"]["lemoncrow"]["limit"]["output"] == 5200
    assert "mcp" not in config
    assert launch.env["LEMONCODE_MANAGED"] == "1"
    assert launch.env["LEMONCODE_STRIP_HOST_PROMPT"] == "1"
    assert launch.env["LEMONCODE_STRIP_HOST_TOOLS"] == "1"
    assert launch.env["OPENCODE_DISABLE_AUTOUPDATE"] == "true"
    assert launch.env["OPENCODE_DISABLE_AUTOCOMPACT"] == "true"
    assert launch.env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"


def test_codex_uses_responses_gateway_and_read_only_outer_sandbox(tmp_path: Path) -> None:
    launch = _launch(tmp_path, "codex", prompt="fix it")
    rendered = " ".join(launch.command)
    assert 'model_provider="lemoncrow"' in rendered
    assert 'wire_api = "responses"' in rendered
    assert "-s read-only" in rendered
    assert rendered.endswith("exec --skip-git-repo-check fix it")


def test_claude_uses_anthropic_gateway_and_empty_outer_mcp(tmp_path: Path) -> None:
    launch = _launch(tmp_path, "claude", resume="session-1")
    assert launch.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:43210"
    assert launch.env["ANTHROPIC_API_KEY"] == "secret"
    assert "--strict-mcp-config" in launch.command
    assert str(tmp_path / "empty.json") in launch.command
    assert launch.command[-2:] == ("--resume", "session-1")
