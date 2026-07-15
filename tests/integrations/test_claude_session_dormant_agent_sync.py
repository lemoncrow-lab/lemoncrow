"""SessionStart's Layer-2 dormant `agent` pop must reach workspace-scoped
installs too.

`install_claude.sh --workspace` writes the `agent` override into
`<workspace>/.claude/settings.json`, not the global `~/.claude/settings.json`
(or `CLAUDE_CONFIG_DIR`). `apply_session_start_files` only ever rewrites the
global file, so without this sync a dormant session would still show
`lemoncrow:code` (mandating `mcp__lc__*` tools the MCP server has already
hidden) for any workspace-scoped install — the exact "persona and MCP not in
sync" symptom this covers.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, cast

import pytest

from integrations.claude.plugin.hooks import session_start

SESSION_START = cast(Any, session_start)


def _patch_saved(monkeypatch: pytest.MonkeyPatch, saved: float) -> None:
    from lemoncrow.core.capabilities import savings_summary

    class _W:
        saved_usd = saved
        spend_usd = 0.0

    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _W())


def _run(payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SESSION_START.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert session_start.main() == 0


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """Workspace with its own .claude/settings.json, distinct from the global config dir."""
    root = tmp_path / "lemoncrow"
    workspace = tmp_path / "ws"
    global_config = tmp_path / "global-claude-config"
    plugin_root = tmp_path / "plugin"
    workspace.mkdir()
    global_config.mkdir()
    plugin_root.mkdir()
    (plugin_root / ".mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(global_config))
    return root, workspace, global_config


def test_dormant_pops_agent_from_workspace_settings_not_just_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, workspace, global_config = _setup(tmp_path, monkeypatch)
    _patch_saved(monkeypatch, 25.0)  # > $20 free cap -> dormant

    project_settings = workspace / ".claude" / "settings.json"
    project_settings.parent.mkdir(parents=True)
    project_settings.write_text(json.dumps({"agent": "lemoncrow:code"}), encoding="utf-8")

    _run({"session_id": "sid-1", "source": "startup", "cwd": str(workspace)}, monkeypatch)

    data = json.loads(project_settings.read_text(encoding="utf-8"))
    assert "agent" not in data  # mirrored pop reaches the workspace-local file
    global_settings = json.loads((global_config / "settings.json").read_text(encoding="utf-8"))
    assert "agent" not in global_settings  # global path still handled as before
    assert root.exists()  # sanity: bootstrap actually ran (not a no-op env)


def test_active_leaves_workspace_agent_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _root, workspace, _global_config = _setup(tmp_path, monkeypatch)
    _patch_saved(monkeypatch, 3.0)  # under cap -> active

    project_settings = workspace / ".claude" / "settings.json"
    project_settings.parent.mkdir(parents=True)
    project_settings.write_text(json.dumps({"agent": "lemoncrow:code"}), encoding="utf-8")

    _run({"session_id": "sid-2", "source": "startup", "cwd": str(workspace)}, monkeypatch)

    data = json.loads(project_settings.read_text(encoding="utf-8"))
    assert data["agent"] == "lemoncrow:code"  # active session keeps the persona


def test_never_clobbers_a_users_custom_workspace_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _root, workspace, _global_config = _setup(tmp_path, monkeypatch)
    _patch_saved(monkeypatch, 25.0)  # dormant

    project_settings = workspace / ".claude" / "settings.json"
    project_settings.parent.mkdir(parents=True)
    project_settings.write_text(json.dumps({"agent": "my:custom"}), encoding="utf-8")

    _run({"session_id": "sid-3", "source": "startup", "cwd": str(workspace)}, monkeypatch)

    data = json.loads(project_settings.read_text(encoding="utf-8"))
    assert data["agent"] == "my:custom"  # never touch a non-lemoncrow agent


def test_noop_when_no_workspace_settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _root, workspace, global_config = _setup(tmp_path, monkeypatch)
    _patch_saved(monkeypatch, 25.0)  # dormant

    _run({"session_id": "sid-4", "source": "startup", "cwd": str(workspace)}, monkeypatch)

    assert not (workspace / ".claude" / "settings.json").exists()  # never created out of thin air
    assert (global_config / "settings.json").exists()  # global bootstrap still ran
