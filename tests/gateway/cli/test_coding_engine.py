from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.gateway.cli.coding_engine import _build_engine_launch, _resolve_engine


def _launch(tmp_path: Path, engine: str, *, prompt: str | None = None, resume: str | None = None):
    return _build_engine_launch(
        engine=engine,
        executable=f"/bin/{engine}",
        base_url="http://127.0.0.1:43210",
        token="secret",
        store_root=tmp_path,
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
    # No "Auto" placeholder -- launches pinned directly to a real, visible
    # model (Zen's keyless free default) instead of blind auto-routing.
    assert config["model"] == "lc/zen/big-pickle"
    assert "--model" in launch.command
    assert launch.command[launch.command.index("--model") + 1] == "lc/zen/big-pickle"
    assert config["enabled_providers"] == ["lc"]
    assert config["share"] == "disabled"
    assert config["snapshot"] is False
    assert config["autoupdate"] is False
    assert config["provider"]["lc"]["options"]["baseURL"].endswith("/v1")
    assert "lemoncrow" not in config["provider"]["lc"]["models"]
    assert config["provider"]["lc"]["models"]["zen/big-pickle"]["limit"]["output"] == 5200
    assert any(key.startswith("zen/") for key in config["provider"]["lc"]["models"])
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


def test_pi_is_isolated_fail_closed_managed_frontend(tmp_path: Path) -> None:
    launch = _launch(tmp_path, "pi", prompt="fix it", resume="session-1")
    rendered = " ".join(launch.command)
    assert launch.command[0] == "/bin/pi"
    for flag in (
        "--offline",
        "--no-tools",
        "--no-context-files",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-approve",
    ):
        assert flag in launch.command
    assert "--provider lc" in rendered
    assert "--model zen/big-pickle" in rendered
    assert "--session session-1" in rendered
    assert rendered.endswith("-p fix it")
    assert launch.env["PI_OFFLINE"] == "1"
    assert launch.env["PI_SKIP_VERSION_CHECK"] == "1"
    assert launch.env["PI_TELEMETRY"] == "0"
    assert launch.env["LEMONCROW_PI_GATEWAY_BASE_URL"] == "http://127.0.0.1:43210/v1"
    assert launch.env["LEMONCROW_PI_GATEWAY_TOKEN"] == "secret"
    assert "OPENCODE_CONFIG_CONTENT" not in launch.env
    config_dir = Path(launch.env["PI_CODING_AGENT_DIR"])
    settings = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
    assert settings["defaultProjectTrust"] == "never"
    assert settings["defaultTools"] == []
    assert settings["compaction"]["enabled"] is False
    assert settings["retry"]["enabled"] is False


def test_auto_prefers_pi_before_lemoncode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.cli import lemoncode_host, pi_host

    monkeypatch.delenv("LEMONCROW_CODE_AUTO_ENGINE", raising=False)
    monkeypatch.setattr(lemoncode_host, "resolve_host_binary", lambda _root: "/fake/lemoncode")
    monkeypatch.setattr(pi_host, "resolve_host_binary", lambda _root: "/fake/pi")
    validated = []
    monkeypatch.setattr(pi_host, "validate_host_binary", lambda path: validated.append(path))
    assert _resolve_engine("auto", store_root=tmp_path) == ("pi", "/fake/pi")
    assert validated == ["/fake/pi"]


def test_auto_engine_override_can_roll_back_to_lemoncode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.cli import lemoncode_host, pi_host

    monkeypatch.setenv("LEMONCROW_CODE_AUTO_ENGINE", "lemoncode")
    monkeypatch.setattr(lemoncode_host, "resolve_host_binary", lambda _root: "/fake/lemoncode")
    monkeypatch.setattr(pi_host, "resolve_host_binary", lambda _root: "/fake/pi")
    monkeypatch.setattr(pi_host, "validate_host_binary", lambda _path: None)
    assert _resolve_engine("auto", store_root=tmp_path) == ("lemoncode", "/fake/lemoncode")


def test_auto_engine_override_rejects_unknown_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_CODE_AUTO_ENGINE", "unknown")
    with pytest.raises(Exception, match="must be lemoncode or pi"):
        _resolve_engine("auto", store_root=tmp_path)


def test_pi_model_catalog_advertises_images_only_for_vision_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.gateway.cli import coding_engine

    monkeypatch.setattr(coding_engine, "_supports_vision", lambda model: model == "openai/gpt-4o")
    monkeypatch.setattr(
        coding_engine,
        "_picker_models",
        lambda _root, _ceiling: {
            "zen/big-pickle": {"name": "zen", "limit": {}},
            "openai/gpt-4o": {"name": "gpt-4o", "limit": {}},
        },
    )
    models = coding_engine._pi_picker_models(tmp_path, 5200)

    assert models["openai/gpt-4o"]["input"] == ["text", "image"]
    assert models["zen/big-pickle"]["input"] == ["text"]


def test_shared_lemoncode_model_catalog_has_no_pi_modality_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.gateway.cli import coding_engine

    monkeypatch.setattr(coding_engine, "_picker_model_entries", lambda _root: [])
    models = coding_engine._picker_models(tmp_path, 5200)
    assert "input" not in models["zen/big-pickle"]
