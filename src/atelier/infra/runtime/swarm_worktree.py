"""Git worktree lifecycle helpers for the Atelier swarm harness."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DirtyPath:
    status: str
    path: str
    source_path: str | None = None

    @property
    def is_delete(self) -> bool:
        return self.source_path is None and "D" in self.status

    @property
    def is_untracked(self) -> bool:
        return self.status == "??"


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"git {' '.join(args)} failed")
    return completed


def git_repo_root(path: Path) -> Path:
    completed = _git(path, "rev-parse", "--show-toplevel")
    return Path(completed.stdout.strip()).resolve()


def read_head_ref(repo_root: Path) -> str:
    return _git(repo_root, "rev-parse", "HEAD").stdout.strip()


def _parse_dirty_line(line: str) -> DirtyPath:
    status = line[:2]
    if "U" in status or status in {"AA", "DD"}:
        raise RuntimeError(f"cannot fan out swarm from conflicted worktree entry: {line}")
    raw_path = line[3:]
    if " -> " in raw_path:
        source_path, target_path = raw_path.split(" -> ", 1)
        return DirtyPath(status=status, path=target_path, source_path=source_path)
    return DirtyPath(status=status, path=raw_path)


def collect_dirty_paths(repo_root: Path) -> list[str]:
    completed = _git(repo_root, "status", "--porcelain")
    return [line.rstrip() for line in completed.stdout.splitlines() if line.strip()]


def collect_dirty_snapshot(repo_root: Path) -> list[DirtyPath]:
    return [_parse_dirty_line(line) for line in collect_dirty_paths(repo_root)]


def _safe_remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


_SKIP_UNTRACKED_DIR_NAMES = frozenset(
    {
        ".atelier-benchmarks",
        ".cache",
        ".codegraph",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "build",
        "coverage",
        "dist",
        "node_modules",
    }
)
_ALLOWLISTED_HIDDEN_DIRS = frozenset({".github", ".lessons", ".planning"})
_SKIP_UNTRACKED_FILE_SUFFIXES = frozenset(
    {
        ".db",
        ".log",
        ".pyc",
        ".pyd",
        ".pyo",
        ".sqlite",
        ".sqlite3",
    }
)
_SKIP_UNTRACKED_FILE_NAMES = frozenset({"semantic_file_index.json"})


def _should_skip_untracked_path(path: str) -> bool:
    parts = Path(path).parts
    if not parts:
        return False
    head = parts[0]
    if head.startswith(".") and head not in _ALLOWLISTED_HIDDEN_DIRS:
        return True
    if any(part in _SKIP_UNTRACKED_DIR_NAMES for part in parts):
        return True
    candidate = Path(path)
    return candidate.name in _SKIP_UNTRACKED_FILE_NAMES or candidate.suffix in _SKIP_UNTRACKED_FILE_SUFFIXES


class SwarmWorktreeManager:
    def __init__(self, *, repo_root: Path, pool_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.pool_root = Path(pool_root).resolve()

    def create_worktree(self, *, run_id: str, child_id: str, ref: str = "HEAD") -> Path:
        path = self.pool_root / child_id
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo_root, "worktree", "add", "--detach", str(path), ref)
        return path

    def sync_dirty_state(self, *, base_worktree: Path, child_worktree: Path) -> None:
        for item in collect_dirty_snapshot(base_worktree):
            if item.is_untracked and _should_skip_untracked_path(item.path):
                continue
            if item.source_path:
                _safe_remove(child_worktree / item.source_path)
            if item.is_delete:
                _safe_remove(child_worktree / item.path)
                continue
            source = base_worktree / item.path
            destination = child_worktree / item.path
            if not source.exists():
                continue
            if source.is_dir():
                _safe_remove(destination)
                shutil.copytree(source, destination, dirs_exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    def remove_worktree(self, worktree_path: Path) -> None:
        path = Path(worktree_path)
        if not path.exists():
            return
        _git(self.repo_root, "worktree", "remove", "--force", str(path))
