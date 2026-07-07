"""Tests for workspace-root resolution and the atelier-init registration guard."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from atelier.core.foundation.paths import (
    DEFAULT_STORE_DIRNAME,
    WorkspaceNotRegisteredError,
    default_store_root,
    resolve_store_root_for_workspace,
    resolve_workspace_root,
)

_HOST_WORKSPACE_ENV_VARS = (
    "ATELIER_WORKSPACE_ROOT",
    "CLAUDE_WORKSPACE_ROOT",
    "CURSOR_WORKSPACE_ROOT",
    "VSCODE_CWD",
)


def _clear_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the conftest autouse fixture's ATELIER_WORKSPACE_ROOT=tmp_path override
    so resolve_workspace_root() actually exercises git/marker/raise resolution
    instead of short-circuiting on tier 1."""
    for env_var in _HOST_WORKSPACE_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)


@pytest.fixture(autouse=True)
def _pin_home_to_tmp_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pin Path.home() to tmp_path so the marker walk can't escape past it into a
    real ancestor directory that might itself carry a stray `.atelier` (e.g. some
    hosts leave one at /tmp/.atelier) -- these tests need a hermetic boundary.
    Tests that need a specific fake home override this again within their body.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def test_unregistered_non_git_dir_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_host_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(WorkspaceNotRegisteredError):
        resolve_workspace_root()


def test_marker_registered_dir_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_host_env(monkeypatch)
    project = tmp_path / "project"
    (project / DEFAULT_STORE_DIRNAME).mkdir(parents=True)
    monkeypatch.chdir(project)
    assert resolve_workspace_root() == project.resolve()


def test_marker_registered_dir_resolves_from_subdirectory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_host_env(monkeypatch)
    project = tmp_path / "project"
    (project / DEFAULT_STORE_DIRNAME).mkdir(parents=True)
    nested = project / "src" / "nested"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert resolve_workspace_root() == project.resolve()


def test_home_subdirectory_without_marker_still_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: a random subdir under $HOME must not spuriously resolve
    just because the global ~/.atelier store dir exists at home -- the marker
    walk must stop before ever treating home itself as a project marker."""
    _clear_host_env(monkeypatch)
    fake_home = tmp_path / "home" / "alice"
    (fake_home / DEFAULT_STORE_DIRNAME).mkdir(parents=True)  # simulates the global store
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    project = fake_home / "some" / "random" / "project"
    project.mkdir(parents=True)
    monkeypatch.chdir(project)
    with pytest.raises(WorkspaceNotRegisteredError):
        resolve_workspace_root()


def test_git_repo_still_auto_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_host_env(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    monkeypatch.chdir(repo)
    assert resolve_workspace_root() == repo.resolve()


def test_store_root_for_workspace_never_raises_for_unregistered_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_host_env(monkeypatch)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    assert resolve_store_root_for_workspace() == default_store_root()


def test_store_root_for_workspace_uses_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_host_env(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    monkeypatch.chdir(repo)
    result = resolve_store_root_for_workspace()
    assert result != default_store_root()
    assert result.parent.name == "workspaces"


def test_store_root_for_workspace_uses_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_host_env(monkeypatch)
    project = tmp_path / "marked"
    (project / DEFAULT_STORE_DIRNAME).mkdir(parents=True)
    monkeypatch.chdir(project)
    result = resolve_store_root_for_workspace()
    assert result != default_store_root()
    assert result.parent.name == "workspaces"
