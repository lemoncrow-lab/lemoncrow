"""Layer-2: dormant swaps the Claude default agent to lemoncrow:free + unloads lc tools.

The swap is written to the HOST settings.json `agent` key (host wins over the
plugin's own settings.json), so no plugin file is mutated.
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
    (p / "settings.json").write_text(json.dumps({"agent": "lemoncrow:code"}), encoding="utf-8")
    (p / ".mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    return p


def _host_agent(config: Path) -> object:
    data = json.loads((config / "settings.json").read_text())
    return data.get("agent", "<unset>")


def test_dormant_sets_host_agent_to_free(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_saved(monkeypatch, 25.0)  # > $20 free cap -> dormant
    root, config, plugin = tmp_path / "root", tmp_path / "cfg", _plugin_root(tmp_path)
    root.mkdir()
    config.mkdir()
    result = pr.apply_session_start_files(root, plugin, config_dir=config)
    assert result["dormant"] is True
    assert "dormant" in result["actions"]
    assert _host_agent(config) == pr.PLUGIN_DORMANT_AGENT  # lemoncrow:free
    # plugin's own settings.json is NOT mutated
    assert json.loads((plugin / "settings.json").read_text())["agent"] == "lemoncrow:code"


def test_active_clears_our_override_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_saved(monkeypatch, 3.0)  # under cap -> active
    root, config, plugin = tmp_path / "root", tmp_path / "cfg", _plugin_root(tmp_path)
    root.mkdir()
    config.mkdir()
    # simulate a prior dormant override sitting in host settings
    (config / "settings.json").write_text(json.dumps({"agent": "lemoncrow:free"}), encoding="utf-8")
    result = pr.apply_session_start_files(root, plugin, config_dir=config)
    assert result["dormant"] is False
    assert "active" in result["actions"]
    assert _host_agent(config) == "<unset>"  # our override cleared -> plugin default applies


def test_active_preserves_user_custom_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_saved(monkeypatch, 3.0)
    root, config, plugin = tmp_path / "root", tmp_path / "cfg", _plugin_root(tmp_path)
    root.mkdir()
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({"agent": "my:custom"}), encoding="utf-8")
    pr.apply_session_start_files(root, plugin, config_dir=config)
    assert _host_agent(config) == "my:custom"  # never clobber a user's own choice
