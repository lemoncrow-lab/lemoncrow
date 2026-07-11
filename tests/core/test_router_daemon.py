from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.core.capabilities import router_daemon as rd


def test_generate_config_passthrough_only(tmp_path: Path) -> None:
    cfg = rd.generate_litellm_config(tmp_path)
    assert cfg["model_list"] == [{"model_name": "*", "litellm_params": {"model": "anthropic/*"}}]


def test_generate_config_with_route_map(tmp_path: Path) -> None:
    rdir = tmp_path / "router"
    rdir.mkdir()
    (rdir / "route.json").write_text(json.dumps({"*opus*": "openai/gpt-5.5", "*haiku*": ""}), encoding="utf-8")
    cfg = rd.generate_litellm_config(tmp_path)
    names = [entry["model_name"] for entry in cfg["model_list"]]
    assert "*opus*" in names  # mapped (non-empty target)
    assert "*haiku*" not in names  # empty target dropped
    assert names[-1] == "*"  # passthrough fallback last


def test_to_yaml_shape() -> None:
    out = rd._to_yaml({"model_list": [{"model_name": "*sonnet*", "litellm_params": {"model": "openai/gpt"}}]})
    assert "model_name: '*sonnet*'" in out
    assert "model: openai/gpt" in out


def test_lifecycle_start_status_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(rd, "_pid_alive", lambda pid: pid == 4242)
    terminated: list[int] = []
    monkeypatch.setattr(rd, "_terminate", lambda pid: terminated.append(pid))
    spawned: dict[str, list[str]] = {}

    def fake_spawn(cmd: list[str]) -> int:
        spawned["cmd"] = cmd
        return 4242

    result = rd.start(tmp_path, port=4010, spawn=fake_spawn)
    assert result["running"] and result["pid"] == 4242 and result["port"] == 4010
    assert spawned["cmd"][0] == "litellm"
    assert rd.daemon_state_path(tmp_path).exists()

    st = rd.status(tmp_path)
    assert st["running"] and st["pid"] == 4242 and st["base_url"] == "http://127.0.0.1:4010"

    again = rd.start(tmp_path, spawn=fake_spawn)
    assert again.get("already_running")

    stopped = rd.stop(tmp_path)
    assert stopped["stopped"] and terminated == [4242]
    assert not rd.daemon_state_path(tmp_path).exists()
    assert rd.status(tmp_path) == {"running": False}


def test_host_wiring_and_restore(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(rd, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(rd, "_terminate", lambda pid: None)
    cfgdir = tmp_path / "claude"
    cfgdir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfgdir))

    rd.start(tmp_path, port=4011, wire_host="claude", spawn=lambda cmd: 4243)
    settings = json.loads((cfgdir / "settings.json").read_text(encoding="utf-8"))
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4011"

    rd.stop(tmp_path)
    restored = json.loads((cfgdir / "settings.json").read_text(encoding="utf-8"))
    assert "ANTHROPIC_BASE_URL" not in restored.get("env", {})  # was absent -> removed


def test_host_wiring_preserves_prior_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(rd, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(rd, "_terminate", lambda pid: None)
    cfgdir = tmp_path / "claude"
    cfgdir.mkdir()
    (cfgdir / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://prior.example"}}), encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfgdir))
    rd.start(tmp_path, port=4012, wire_host="claude", spawn=lambda cmd: 4244)
    rd.stop(tmp_path)
    restored = json.loads((cfgdir / "settings.json").read_text(encoding="utf-8"))
    assert restored["env"]["ANTHROPIC_BASE_URL"] == "https://prior.example"
