"""A commit holding two paths that differ only in case still freezes on a case-insensitive disk."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from lemoncrow_client.review import _materialize_commit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def test_case_colliding_paths_freeze(tmp_path: Path) -> None:
    probe = tmp_path / "Probe"
    probe.write_text("")
    if not (tmp_path / "probe").exists():
        pytest.skip("case-sensitive disk: the collision cannot happen here")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, input="same\n", check=True, capture_output=True, text=True
    ).stdout.strip()
    for name in ("PRODUCT.md", "product.md"):
        _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    tree = _git(repo, "write-tree")
    commit = _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit-tree", tree, "-m", "c")
    target = tmp_path / "head"
    _materialize_commit(repo, commit, target)
    assert (target / "product.md").read_text() == "same\n"
