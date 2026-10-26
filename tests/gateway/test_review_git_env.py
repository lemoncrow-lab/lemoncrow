"""A git command meant for a throwaway repository never reaches the repository the caller is standing in.

A process started by a git hook, ``git rebase -x`` or ``git bisect run`` inherits ``GIT_DIR`` and its relatives, and
they win over ``cwd`` and ``-C``. A test that ran ``git init`` and ``git config`` in a temporary directory then rewrote the
config of the real repository (and of every worktree sharing it). Each test here points those variables at a
throwaway "victim" repository and checks that the victim is left exactly as it was.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.evaluation import golden_comments, label_derivation
from lemoncrow.pro.capabilities.review.evaluation.benchmark import Workspace
from lemoncrow.pro.capabilities.review.evaluation.real_prs import load_real_pr_corpus
from lemoncrow.pro.capabilities.review.git_env import REPOSITORY_LOCATION_VARIABLES, git_environment
from tests.gateway.benchmark_support import make_origin, write_corpus
from tests.gateway.golden_support import make_key

REPO_ROOT = Path(__file__).resolve().parents[2]
_CLEAN = {"GIT_CONFIG_NOSYSTEM": "1"}


def run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=git_environment(_CLEAN)
    ).stdout.strip()


class Victim:
    """A repository that must not change: its config, its HEAD, its refs and its index."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        run_git(root, "init", "-q", "-b", "victim")
        run_git(root, "config", "user.name", "T")
        run_git(root, "config", "user.email", "t@example.invalid")
        (root / "keep.txt").write_text("keep\n", encoding="utf-8")
        run_git(root, "add", "-A")
        run_git(root, "commit", "-qm", "victim")
        self.before = self.snapshot()

    def snapshot(self) -> dict[str, str]:
        git_dir = self.root / ".git"
        return {
            "config": (git_dir / "config").read_text(encoding="utf-8"),
            "head": (git_dir / "HEAD").read_text(encoding="utf-8"),
            "refs": run_git(self.root, "for-each-ref"),
            "log": run_git(self.root, "log", "--format=%H"),
            "status": run_git(self.root, "status", "--porcelain"),
        }

    def point_environment_at_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GIT_DIR", str(self.root / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(self.root))
        monkeypatch.setenv("GIT_INDEX_FILE", str(self.root / ".git" / "index"))
        monkeypatch.setenv("GIT_COMMON_DIR", str(self.root / ".git"))

    def untouched(self) -> bool:
        return self.snapshot() == self.before


def test_the_git_environment_drops_the_variables_that_choose_a_repository() -> None:
    env = git_environment({"GIT_AUTHOR_NAME": "Me"})
    assert not set(REPOSITORY_LOCATION_VARIABLES) & set(env)
    assert env["GIT_AUTHOR_NAME"] == "Me" and env.get("PATH") == os.environ.get("PATH")
    assert (
        git_environment({"GIT_INDEX_FILE": "/scratch/index"})["GIT_INDEX_FILE"] == "/scratch/index"
    )  # chosen on purpose
    assert set(REPOSITORY_LOCATION_VARIABLES) >= {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"}


def test_a_test_origin_built_while_git_dir_is_inherited_leaves_that_repository_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = Victim(tmp_path / "victim")
    victim.point_environment_at_it(monkeypatch)
    origin = make_origin(tmp_path / "w")
    assert run_git(origin.path, "log", "--format=%s").splitlines()[-1] == "base"  # the origin got its own five commits
    assert victim.untouched()


def test_the_benchmark_workspace_is_built_in_its_own_directory_not_in_the_inherited_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = make_origin(tmp_path / "w")
    pr = load_real_pr_corpus(write_corpus(tmp_path, origin)).prs[0]
    victim = Victim(tmp_path / "victim")
    victim.point_environment_at_it(monkeypatch)
    workspace = Workspace(pr, tmp_path / "work")
    workspace.prepare()  # git init, remote add, fetch by id: every one of them used to follow GIT_DIR
    assert workspace.has(pr.head_sha) and (workspace.repo / ".git" / "config").is_file()
    assert "origin" in (workspace.repo / ".git" / "config").read_text(encoding="utf-8")
    assert victim.untouched()
    quick = workspace.with_policy(pr.base_sha, pr.head_sha, "review:\n  effort: quick\n")
    assert len(quick) == 2 and victim.untouched()  # the scratch index is chosen on purpose and still works


def test_reading_another_repository_reads_it_not_the_inherited_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_dir, commit = make_key(tmp_path)
    victim = Victim(tmp_path / "victim")
    victim.point_environment_at_it(monkeypatch)
    assert golden_comments._git(key_dir, "rev-parse", "HEAD") == commit
    assert label_derivation._git(key_dir, "rev-parse", "HEAD").strip() == commit
    assert victim.untouched()


def test_the_suite_itself_removes_the_variables_before_any_test_runs(tmp_path: Path) -> None:
    """A child pytest started with GIT_DIR set sees none of the repository-location variables inside a test."""

    victim = Victim(tmp_path / "victim")
    env = {**os.environ, **{name: str(victim.root / ".git") for name in ("GIT_DIR", "GIT_COMMON_DIR")}}
    env["GIT_WORK_TREE"] = str(victim.root)
    env["GIT_INDEX_FILE"] = str(victim.root / ".git" / "index")
    test = "tests/gateway/test_review_git_env.py::test_inside_a_test_no_variable_names_a_repository"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", test, "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-500:]
    assert "1 passed" in done.stdout
    assert victim.untouched()


def test_inside_a_test_no_variable_names_a_repository() -> None:
    named = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_PREFIX",
    }
    assert not named & set(os.environ)
    assert named == set(REPOSITORY_LOCATION_VARIABLES)
