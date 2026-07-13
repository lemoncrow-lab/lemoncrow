"""Layer-2: dormant swaps the Claude default agent to host default + unloads lc tools."""

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


def test_reconcile_agent_dormant_pops_active_restores() -> None:
    import tempfile

    d = Path(tempfile.mkdtemp())
    (d / "settings.json").write_text(json.dumps({"agent": "lemoncrow:code", "x": 1}), encoding="utf-8")
    assert pr.reconcile_dormant_agent(d, dormant=True) is True
    assert "agent" not in json.loads((d / "settings.json").read_text())
    assert pr.reconcile_dormant_agent(d, dormant=True) is False  # idempotent
    assert pr.reconcile_dormant_agent(d, dormant=False) is True
    assert json.loads((d / "settings.json").read_text())["agent"] == "lemoncrow:code"


def test_bootstrap_dormant_true_and_tools_unloaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_saved(monkeypatch, 25.0)  # > $20 free cap -> dormant
    root = tmp_path / "root"
    root.mkdir()
    config = tmp_path / "cfg"
    config.mkdir()
    plugin = _plugin_root(tmp_path)
    result = pr.apply_session_start_files(root, plugin, config_dir=config)
    assert result["dormant"] is True
    assert "dormant" in result["actions"]
    # plugin default agent removed -> host default takes over next session
    assert "agent" not in json.loads((plugin / "settings.json").read_text())


def test_bootstrap_active_keeps_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_saved(monkeypatch, 3.0)  # under cap -> active
    root = tmp_path / "root"
    root.mkdir()
    config = tmp_path / "cfg"
    config.mkdir()
    plugin = _plugin_root(tmp_path)
    result = pr.apply_session_start_files(root, plugin, config_dir=config)
    assert result["dormant"] is False
    assert "active" in result["actions"]
    assert json.loads((plugin / "settings.json").read_text())["agent"] == "lemoncrow:code"
