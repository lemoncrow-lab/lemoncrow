from __future__ import annotations

import subprocess
from pathlib import Path

from atelier.infra.runtime.swarm_worktree import SwarmWorktreeManager


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
