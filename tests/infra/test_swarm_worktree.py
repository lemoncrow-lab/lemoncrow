from __future__ import annotations

import subprocess
from pathlib import Path

from lemoncrow.infra.runtime.swarm_worktree import SwarmWorktreeManager


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", ".")
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            message,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_worktree_manager_copies_dirty_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    (repo / "delete-me.txt").write_text("remove\n", encoding="utf-8")
    _commit_all(repo, "base")

    (repo / "tracked.txt").write_text("updated\n", encoding="utf-8")
    (repo / "delete-me.txt").unlink()
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    manager = SwarmWorktreeManager(repo_root=repo, pool_root=tmp_path / "pool")
    child = manager.create_worktree(run_id="swarm-test", child_id="run-01")
    manager.sync_dirty_state(base_worktree=repo, child_worktree=child)

    assert (child / "tracked.txt").read_text(encoding="utf-8") == "updated\n"
    assert not (child / "delete-me.txt").exists()
    assert (child / "new.txt").read_text(encoding="utf-8") == "new\n"

    manager.remove_worktree(child)
    assert not child.exists()


def test_worktree_manager_copies_untracked_directories(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _commit_all(repo, "base")

    nested = repo / "scratch" / "nested"
    nested.mkdir(parents=True)
    (nested / "note.txt").write_text("hello\n", encoding="utf-8")

    manager = SwarmWorktreeManager(repo_root=repo, pool_root=tmp_path / "pool")
    child = manager.create_worktree(run_id="swarm-test", child_id="run-01")
    manager.sync_dirty_state(base_worktree=repo, child_worktree=child)

    assert (child / "scratch" / "nested" / "note.txt").read_text(encoding="utf-8") == "hello\n"

    manager.remove_worktree(child)
    assert not child.exists()


def test_worktree_manager_skips_generated_untracked_directories(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _commit_all(repo, "base")

    generated = repo / ".codegraph" / "cache"
    generated.mkdir(parents=True)
    (generated / "index.json").write_text('{"huge": true}\n', encoding="utf-8")
    useful = repo / "scratch" / "nested"
    useful.mkdir(parents=True)
    (useful / "note.txt").write_text("hello\n", encoding="utf-8")

    manager = SwarmWorktreeManager(repo_root=repo, pool_root=tmp_path / "pool")
    child = manager.create_worktree(run_id="swarm-test", child_id="run-01")
    manager.sync_dirty_state(base_worktree=repo, child_worktree=child)

    assert not (child / ".codegraph").exists()
    assert (child / "scratch" / "nested" / "note.txt").read_text(encoding="utf-8") == "hello\n"

    manager.remove_worktree(child)
    assert not child.exists()


def test_worktree_manager_copies_allowlisted_hidden_directories(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _commit_all(repo, "base")

    planning = repo / ".planning" / "notes"
    planning.mkdir(parents=True)
    (planning / "todo.md").write_text("keep this\n", encoding="utf-8")

    manager = SwarmWorktreeManager(repo_root=repo, pool_root=tmp_path / "pool")
    child = manager.create_worktree(run_id="swarm-test", child_id="run-01")
    manager.sync_dirty_state(base_worktree=repo, child_worktree=child)

    assert (child / ".planning" / "notes" / "todo.md").read_text(encoding="utf-8") == "keep this\n"

    manager.remove_worktree(child)
    assert not child.exists()


def test_worktree_manager_copies_secret_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _commit_all(repo, "base")

    # Add secret files (gitignored) after the commit
    (repo / ".gitignore").write_text(".env\n.env.*\ncredentials.json\n*.pem\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=abc\n", encoding="utf-8")
    (repo / ".env.local").write_text("LOCAL=xyz\n", encoding="utf-8")
    subdir = repo / "subdir"
    subdir.mkdir()
    (subdir / "credentials.json").write_text('{"key": "val"}\n', encoding="utf-8")
    (repo / "cert.pem").write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")

    manager = SwarmWorktreeManager(repo_root=repo, pool_root=tmp_path / "pool")
    child = manager.create_worktree(run_id="swarm-test", child_id="run-01")
    manager.sync_dirty_state(base_worktree=repo, child_worktree=child)

    assert (child / ".env").read_text(encoding="utf-8") == "SECRET=abc\n"
    assert (child / ".env.local").read_text(encoding="utf-8") == "LOCAL=xyz\n"
    assert (child / "subdir" / "credentials.json").read_text(encoding="utf-8") == '{"key": "val"}\n'
    assert (child / "cert.pem").read_text(encoding="utf-8") == "-----BEGIN CERTIFICATE-----\n"

    manager.remove_worktree(child)
    assert not child.exists()
