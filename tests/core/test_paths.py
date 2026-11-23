"""Tests for workspace-root resolution and the lemoncrow-init registration guard."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lemoncrow.core.foundation.paths import (
    DEFAULT_STORE_DIRNAME,
    WorkspaceNotRegisteredError,
    default_store_root,
    resolve_store_root_for_workspace,
    resolve_workspace_root,
)

_HOST_WORKSPACE_ENV_VARS = (
    "LEMONCROW_WORKSPACE_ROOT",
    "CLAUDE_WORKSPACE_ROOT",
    "CURSOR_WORKSPACE_ROOT",
    "VSCODE_CWD",
)


def _clear_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the conftest autouse fixture's LEMONCROW_WORKSPACE_ROOT=tmp_path override
    so resolve_workspace_root() actually exercises git/marker/raise resolution
    instead of short-circuiting on tier 1."""
    for env_var in _HOST_WORKSPACE_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)


@pytest.fixture(autouse=True)
def _pin_home_to_tmp_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pin Path.home() to tmp_path so the marker walk can't escape past it into a
    real ancestor directory that might itself carry a stray `.lemoncrow` (e.g. some
    hosts leave one at /tmp/.lemoncrow) -- these tests need a hermetic boundary.
    Tests that need a specific fake home override this again within their body.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.mark.parametrize("marker", ["CLAUDE_CODE", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"])
def test_claude_runtime_markers_beat_inherited_opencode(marker: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.foundation.paths import detect_host, session_dir

    monkeypatch.delenv("LEMONCROW_AGENT", raising=False)
    for name in ("CLAUDE_CODE", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(marker, "claude-session" if marker.endswith("SESSION_ID") else "1")
    monkeypatch.setenv("OPENCODE_SESSION_ID", "inherited-opencode-session")
    monkeypatch.setenv("OPENCODE_CLI", "1")

    host = detect_host()
    assert host == "claude"
    assert session_dir(Path.home(), host, "claude-session").parts[-2:] == ("claude", "claude-session")


def test_explicit_host_override_keeps_precedence_over_claude_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.foundation.paths import detect_host

    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("LEMONCROW_AGENT", "opencode")
    assert detect_host() == "opencode"


def test_claude_session_id_rejects_stale_opencode_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.foundation.paths import detect_host

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-window")
    monkeypatch.setenv("LEMONCROW_AGENT", "opencode")
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    assert detect_host() == "claude"

    monkeypatch.setenv("OPENCODE_SESSION_ID", "nested-opencode-session")
    assert detect_host() == "opencode"


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
    just because the global ~/.lemoncrow store dir exists at home -- the marker
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
    assert result.parent.name == DEFAULT_STORE_DIRNAME
    assert result.name == "workspace"
    assert result.parent.parent == repo.resolve()


def test_store_root_for_workspace_uses_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_host_env(monkeypatch)
    project = tmp_path / "marked"
    (project / DEFAULT_STORE_DIRNAME).mkdir(parents=True)
    monkeypatch.chdir(project)
    result = resolve_store_root_for_workspace()
    assert result != default_store_root()
    assert result.parent.name == DEFAULT_STORE_DIRNAME
    assert result.name == "workspace"
    assert result.parent.parent == project.resolve()


def test_store_bundle_builds_in_unregistered_non_git_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Global read-only surfaces (`lc savings`, ...) construct the store bundle just
    to read store-root data -- that must not require a git repo or `lc init`."""
    _clear_host_env(monkeypatch)
    monkeypatch.delenv("LEMONCROW_LESSONS_ROOT", raising=False)
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    from lemoncrow.infra.storage.factory import create_store

    # The global store root (~/.lemoncrow, home pinned to tmp_path here) -- the
    # exact root the CLI passes, and one that carries no workspace hint.
    store_root = tmp_path / DEFAULT_STORE_DIRNAME
    store = create_store(store_root)
    assert store.knowledge.blocks_dir == store_root / "blocks"


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_workspace_store_dir_never_shows_up_in_git_status(tmp_path: Path) -> None:
    """Reviewing a repo must not add a line to that repo's `git status`.

    Everything under `<repo>/.lemoncrow/` is runtime data LemonCrow wrote for
    itself -- the code index, `workspace/*.sqlite`, session state, review
    artifacts. A tool that reports on a change while adding `?? .lemoncrow/` to
    the change it is reporting on is lying by addition, so the self-ignore is
    written where every workspace artifact path is derived rather than by each
    writer. One `.gitignore` at the `.lemoncrow/` level is what makes the cover
    total: a future artifact nobody has written yet is already ignored.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "a.py"], repo)
    _git(["commit", "-qm", "init"], repo)

    from lemoncrow.core.foundation.paths import resolve_workspace_store_dir

    store_dir = resolve_workspace_store_dir(workspace_root=repo)
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "code_context.sqlite").write_bytes(b"not really sqlite")
    (store_dir / "zoekt").mkdir(parents=True, exist_ok=True)
    (store_dir / "zoekt" / "shard.zoekt").write_bytes(b"shard")

    assert store_dir.parent == repo / DEFAULT_STORE_DIRNAME
    assert _git(["status", "--porcelain"], repo) == ""


def test_the_committed_team_config_is_not_ignored(tmp_path: Path) -> None:
    """Every entry the edit tool treats as committed config must also reach ``git add``."""
    from lemoncrow_client.kit.edit import COMMITTED_CONFIG

    from lemoncrow.core.foundation.paths import ensure_gitignore

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    ensure_gitignore(repo)
    config = [f"{entry}team.md" if entry.endswith("/") else entry for entry in COMMITTED_CONFIG]
    state = ["workspace/code_context.sqlite", "environment.toml.bak", "rules.md"]
    for relative in [*config, *state]:
        path = repo / DEFAULT_STORE_DIRNAME / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")

    untracked = _git(["ls-files", "--others", "--exclude-standard", DEFAULT_STORE_DIRNAME], repo).split()

    assert sorted(untracked) == sorted(f"{DEFAULT_STORE_DIRNAME}/{relative}" for relative in config)
