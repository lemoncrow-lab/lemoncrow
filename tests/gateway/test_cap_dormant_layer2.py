"""Layer-2: dormant UNSETS the host `agent` (no fallback agent; tool-hiding enforces).

Enforcement is server-side (the MCP server hides all lc tools when dormant), so
the agent side just clears any host `agent` override -> the model runs on the
host default persona with built-in tools. It must never clobber a user's own
custom agent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.core.capabilities import plugin_runtime as pr


def _patch_saved(monkeypatch: pytest.MonkeyPatch, saved: float) -> None:
    from lemoncrow.core.capabilities import savings_summary

    class _W:
        saved_usd = saved
        spend_usd = 0.0

    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _W())


def _plugin_root(tmp_path: Path) -> Path:
    p = tmp_path / "plugin"
    p.mkdir()
    (p / ".mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    return p


def _host_agent(config: Path) -> object:
    data = json.loads((config / "settings.json").read_text())
    return data.get("agent", "<unset>")


def test_dormant_unsets_host_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_saved(monkeypatch, 25.0)  # > $20 free cap -> dormant
    root, config, plugin = tmp_path / "root", tmp_path / "cfg", _plugin_root(tmp_path)
    root.mkdir()
    config.mkdir()
    result = pr.apply_session_start_files(root, plugin, config_dir=config)
    assert result["dormant"] is True
    assert _host_agent(config) == "<unset>"  # unset -> host default; no lc tools visible


def test_dormant_clears_stale_free_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_saved(monkeypatch, 3.0)  # under cap -> active
    root, config, plugin = tmp_path / "root", tmp_path / "cfg", _plugin_root(tmp_path)
    root.mkdir()
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({"agent": "lemoncrow:free"}), encoding="utf-8")
    pr.apply_session_start_files(root, plugin, config_dir=config)
    assert _host_agent(config) == "<unset>"  # stale free override from older build removed


def test_active_preserves_user_custom_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_saved(monkeypatch, 3.0)
    root, config, plugin = tmp_path / "root", tmp_path / "cfg", _plugin_root(tmp_path)
    root.mkdir()
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({"agent": "my:custom"}), encoding="utf-8")
    pr.apply_session_start_files(root, plugin, config_dir=config)
    assert _host_agent(config) == "my:custom"  # never clobber a user's own choice


# clear_dormant_agent_override: mirrors the same Layer-2 pop into a settings.json
# that isn't the host's global config (e.g. a project-local .claude/settings.json
# written by `install_claude.sh --workspace`, which apply_session_start_files's
# config_dir write never touches).


def test_clear_dormant_agent_override_pops_lemoncrow_agent(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"agent": "lemoncrow:code", "other": True}), encoding="utf-8")
    assert pr.clear_dormant_agent_override(path, dormant=True) is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "agent" not in data
    assert data["other"] is True


def test_clear_dormant_agent_override_active_clears_only_stale_free(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"agent": "lemoncrow:free"}), encoding="utf-8")
    assert pr.clear_dormant_agent_override(path, dormant=False) is True
    assert "agent" not in json.loads(path.read_text(encoding="utf-8"))


def test_clear_dormant_agent_override_active_leaves_non_free_agent(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"agent": "lemoncrow:code"}), encoding="utf-8")
    assert pr.clear_dormant_agent_override(path, dormant=False) is False
    assert json.loads(path.read_text(encoding="utf-8"))["agent"] == "lemoncrow:code"


def test_clear_dormant_agent_override_never_touches_custom_agent(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"agent": "my:custom"}), encoding="utf-8")
    assert pr.clear_dormant_agent_override(path, dormant=True) is False
    assert json.loads(path.read_text(encoding="utf-8"))["agent"] == "my:custom"


def test_clear_dormant_agent_override_missing_file_is_noop(tmp_path: Path) -> None:
    assert pr.clear_dormant_agent_override(tmp_path / "nope.json", dormant=True) is False
