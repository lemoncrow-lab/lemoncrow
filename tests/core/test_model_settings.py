from __future__ import annotations

import json
import subprocess
from pathlib import Path

from lemoncrow.core.capabilities import model_settings
from lemoncrow.core.capabilities.model_settings import (
    global_model_settings_path,
    load_model_settings,
    resolve_explicit_host_model,
    resolve_host_model,
    resolve_runtime_model,
    workspace_model_settings_path,
)


def test_workspace_settings_override_global(tmp_path: Path, monkeypatch) -> None:
    global_root = tmp_path / "global-root"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(global_root))
    global_path = global_model_settings_path()
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(
        json.dumps(
            {
                "models": {
                    "runtime": {"roles": {"code": "gpt-5.4"}},
                    "hosts": {"copilot": {"roles": {"code": "gpt-5.4"}}},
                }
            }
        ),
        encoding="utf-8",
    )
    local_path = workspace_model_settings_path(workspace)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(
        json.dumps(
            {
                "models": {
                    "runtime": {"roles": {"code": "claude-opus-4.8"}},
                    "hosts": {"copilot": {"roles": {"code": "claude-opus-4.8"}}},
                }
            }
        ),
        encoding="utf-8",
    )

    merged = load_model_settings(workspace)
    assert merged["models"]["runtime"]["roles"]["code"] == "claude-opus-4.8"
    assert resolve_runtime_model("code", workspace) == "claude-opus-4.8"
    assert resolve_host_model("copilot", "code", workspace_root=workspace) == "claude-opus-4.8"


def test_host_auto_resolves_to_no_explicit_model(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    local_path = workspace_model_settings_path(workspace)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(
        json.dumps({"models": {"hosts": {"claude": {"roles": {"code": "auto"}}}}}),
        encoding="utf-8",
    )

    assert resolve_host_model("claude", "code", workspace_root=workspace) is None
    assert resolve_runtime_model("code", workspace) == "claude-opus-4.8"


def test_host_without_override_inherits_runtime_model(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    local_path = workspace_model_settings_path(workspace)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(
        json.dumps({"models": {"runtime": {"roles": {"code": "gpt-5.5"}}}}),
        encoding="utf-8",
    )

    assert resolve_host_model("opencode", "code", workspace_root=workspace) == "gpt-5.5"


def test_legacy_all_auto_host_stub_inherits_runtime_model(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    local_path = workspace_model_settings_path(workspace)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(
        json.dumps(
            {
                "models": {
                    "runtime": {"roles": {"code": "gpt-5.5"}},
                    "hosts": {
                        "claude": {
                            "roles": {
                                "code": "auto",
                                "execute": "auto",
                                "explore": "auto",
                                "plan": "auto",
                                "research": "auto",
                                "review": "auto",
                                "solve": "auto",
                            }
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    assert resolve_host_model("claude", "code", workspace_root=workspace) == "gpt-5.5"


def test_shipped_host_default_pins_explore_and_research_cheap(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    assert resolve_explicit_host_model("claude", "explore", workspace_root=workspace) == "haiku"
    assert resolve_explicit_host_model("claude", "research", workspace_root=workspace) == "haiku"
    assert resolve_explicit_host_model("codex", "explore", workspace_root=workspace) == "gpt-5.4-mini"
    # Coding/judgment roles inherit the session model (no pin).
    assert resolve_explicit_host_model("claude", "code", workspace_root=workspace) is None
    assert resolve_explicit_host_model("claude", "plan", workspace_root=workspace) is None


def test_codex_shipped_default_prefers_live_discovery(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))
    monkeypatch.setattr(model_settings, "_discover_codex_mini_model", lambda: "gpt-6.1-mini")

    assert resolve_explicit_host_model("codex", "explore", workspace_root=workspace) == "gpt-6.1-mini"


def test_codex_shipped_default_falls_back_when_discovery_unavailable(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))
    monkeypatch.setattr(model_settings, "_discover_codex_mini_model", lambda: None)

    assert resolve_explicit_host_model("codex", "explore", workspace_root=workspace) == "gpt-5.4-mini"


def test_codex_live_discovery_does_not_override_explicit_user_setting(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))
    monkeypatch.setattr(model_settings, "_discover_codex_mini_model", lambda: "gpt-6.1-mini")
    local_path = workspace_model_settings_path(workspace)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(
        json.dumps({"models": {"hosts": {"codex": {"roles": {"explore": "gpt-4.9-mini"}}}}}),
        encoding="utf-8",
    )

    assert resolve_explicit_host_model("codex", "explore", workspace_root=workspace) == "gpt-4.9-mini"


def test_discover_codex_mini_model_picks_lowest_priority_list_visible_mini(monkeypatch) -> None:
    model_settings._discover_codex_mini_model.cache_clear()
    monkeypatch.setattr(model_settings.shutil, "which", lambda name: "/usr/bin/codex")
    catalog = json.dumps(
        {
            "models": [
                {"slug": "gpt-6", "priority": 1, "visibility": "list"},
                {"slug": "gpt-6-mini", "priority": 20, "visibility": "list"},
                {"slug": "gpt-6-nano-mini", "priority": 5, "visibility": "hide"},
                {"slug": "gpt-5.4-mini", "priority": 23, "visibility": "list"},
            ]
        }
    )

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=catalog, stderr="")

    monkeypatch.setattr(model_settings.subprocess, "run", _fake_run)

    try:
        assert model_settings._discover_codex_mini_model() == "gpt-6-mini"
    finally:
        model_settings._discover_codex_mini_model.cache_clear()


def test_discover_codex_mini_model_returns_none_when_codex_not_installed(monkeypatch) -> None:
    model_settings._discover_codex_mini_model.cache_clear()
    monkeypatch.setattr(model_settings.shutil, "which", lambda name: None)

    try:
        assert model_settings._discover_codex_mini_model() is None
    finally:
        model_settings._discover_codex_mini_model.cache_clear()


def test_discover_codex_mini_model_returns_none_on_bad_json(monkeypatch) -> None:
    model_settings._discover_codex_mini_model.cache_clear()
    monkeypatch.setattr(model_settings.shutil, "which", lambda name: "/usr/bin/codex")

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="not json", stderr="")

    monkeypatch.setattr(model_settings.subprocess, "run", _fake_run)

    try:
        assert model_settings._discover_codex_mini_model() is None
    finally:
        model_settings._discover_codex_mini_model.cache_clear()


def test_explicit_auto_overrides_shipped_host_default(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))
    local_path = workspace_model_settings_path(workspace)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(
        json.dumps({"models": {"hosts": {"claude": {"roles": {"explore": "auto"}}}}}),
        encoding="utf-8",
    )

    assert resolve_explicit_host_model("claude", "explore", workspace_root=workspace) is None
