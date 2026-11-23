"""A bare ``lc review`` diffs against the remote copy of the branch: unpushed commits count."""

from __future__ import annotations

import subprocess
from pathlib import Path

from lemoncrow.pro.capabilities.review.gitdiff import default_review_base


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True, capture_output=True)
    for key, value in (("user.name", "A"), ("user.email", "a@example.invalid"), ("commit.gpgsign", "false")):
        _git(repo, "config", key, value)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "push", "-q", "origin", "main")
    return repo, remote


def test_nothing_unpushed_has_no_default_base(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    assert default_review_base(repo) == ""


def test_an_unpushed_commit_is_diffed_from_where_the_branch_leaves_the_remote(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    pushed = _git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "local")
    assert default_review_base(repo) == pushed


def test_a_branch_never_pushed_forks_from_the_default_branch(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    fork = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "b.py").write_text("y = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feature work")
    assert default_review_base(repo) == fork


def test_no_remote_means_head(tmp_path: Path) -> None:
    repo = tmp_path / "solo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for key, value in (("user.name", "A"), ("user.email", "a@example.invalid"), ("commit.gpgsign", "false")):
        _git(repo, "config", key, value)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    assert default_review_base(repo) == ""
