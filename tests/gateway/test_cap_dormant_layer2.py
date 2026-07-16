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
    _patch_saved(monkeypatch, 55.0)  # > $50 anonymous cap -> dormant
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
    assert _host_agent(config) == "lemoncrow:code"  # stale free override upgraded, not just unset


def test_active_restores_lemoncrow_code_after_a_prior_dormant_pop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # integrations/claude/plugin/settings.json deliberately carries no static
    # `agent` default (it used to, and that re-asserted lemoncrow:code even
    # while dormant) -- this write is now the ONLY thing that ever restores
    # the persona once a prior dormant session popped it.
    _patch_saved(monkeypatch, 3.0)  # under cap -> active
    root, config, plugin = tmp_path / "root", tmp_path / "cfg", _plugin_root(tmp_path)
    root.mkdir()
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({}), encoding="utf-8")  # popped by an earlier dormant session
    pr.apply_session_start_files(root, plugin, config_dir=config)
    assert _host_agent(config) == "lemoncrow:code"


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
    assert json.loads(path.read_text(encoding="utf-8"))["agent"] == "lemoncrow:code"


def test_clear_dormant_agent_override_active_restores_after_a_prior_pop(tmp_path: Path) -> None:
    # A file WE popped during dormancy carries the ownership marker, so a later
    # active session restores lemoncrow:code and drops the marker.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"other": True, pr._AGENT_OWNERSHIP_MARKER: True}), encoding="utf-8")
    assert pr.clear_dormant_agent_override(path, dormant=False) is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["agent"] == "lemoncrow:code"
    assert data["other"] is True
    assert pr._AGENT_OWNERSHIP_MARKER not in data  # marker cleared on restore


def test_clear_dormant_agent_override_ignores_foreign_settings_without_marker(tmp_path: Path) -> None:
    # Absent agent key AND no ownership marker = a foreign file (a repo's own
    # committed settings.json that never did a workspace install). Never inject
    # our agent into it, in either direction -- that dirties a git-tracked file
    # and points teammates at an agent they don't have.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"permissions": {"allow": ["Bash"]}}), encoding="utf-8")
    assert pr.clear_dormant_agent_override(path, dormant=False) is False
    assert pr.clear_dormant_agent_override(path, dormant=True) is False
    assert json.loads(path.read_text(encoding="utf-8")) == {"permissions": {"allow": ["Bash"]}}


def test_clear_dormant_agent_override_pop_then_restore_round_trip(tmp_path: Path) -> None:
    # Full owned-file cycle: pop (dormant) stamps the marker; restore (active)
    # brings the agent back and removes the marker, leaving a clean file.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"agent": "lemoncrow:code"}), encoding="utf-8")
    assert pr.clear_dormant_agent_override(path, dormant=True) is True
    popped = json.loads(path.read_text(encoding="utf-8"))
    assert "agent" not in popped and popped[pr._AGENT_OWNERSHIP_MARKER] is True
    assert pr.clear_dormant_agent_override(path, dormant=False) is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"agent": "lemoncrow:code"}


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
