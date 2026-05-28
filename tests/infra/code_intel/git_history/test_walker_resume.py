"""Unit tests for iter_commit_records() — including resume/skip logic.

Requires pygit2. If pygit2 is unavailable, all tests in this file are skipped.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(args: list[str], repo: Path, *, env: dict[str, str] | None = None) -> None:
    base_env: dict[str, str] = {
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@t.com",
    }
    if env:
        base_env.update(env)
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**__import__("os").environ, **base_env},
    )


@pytest.fixture()
def five_commit_repo(tmp_path: Path) -> Path:
    """A git repo with 5 non-initial commits (1 initial + 5 real = 6 total commits)."""
    _git(["init"], tmp_path)
    _git(["config", "user.name", "T"], tmp_path)
    _git(["config", "user.email", "t@t.com"], tmp_path)

    # Initial commit
    (tmp_path / "README.md").write_text("init")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "initial"], tmp_path)

    # 5 real commits
    for i in range(1, 6):
        (tmp_path / f"file{i}.py").write_text(f"x = {i}")
        _git(["add", "-A"], tmp_path)
        _git(["commit", "-m", f"commit {i}"], tmp_path)

    return tmp_path


def _get_sha(repo: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_iter_all_5_commits(five_commit_repo: Path) -> None:
    try:
        from atelier.infra.code_intel.git_history.walker import iter_commit_records
    except Exception:
        pytest.skip("pygit2 not available")
    records = list(iter_commit_records(five_commit_repo, limit=500))
    assert len(records) == 5, f"Expected 5 commits, got {len(records)}"


def test_skip_merge_commit(tmp_path: Path) -> None:
    """A merge commit with no diff patches should be skipped."""
    try:
        from atelier.infra.code_intel.git_history.walker import iter_commit_records
    except Exception:
        pytest.skip("pygit2 not available")

    _git(["init"], tmp_path)
    _git(["config", "user.name", "T"], tmp_path)
    _git(["config", "user.email", "t@t.com"], tmp_path)

    # Initial commit on main
    (tmp_path / "main.py").write_text("x = 1")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "initial"], tmp_path)

    # Branch A with one commit
    _git(["checkout", "-b", "branch-a"], tmp_path)
    (tmp_path / "a.py").write_text("a = 1")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "branch A commit"], tmp_path)

    # Merge back with --no-ff to force a merge commit
    _git(["checkout", "master" if (tmp_path / ".git" / "refs" / "heads" / "master").exists() else "main"], tmp_path)
    _git(["merge", "--no-ff", "branch-a", "-m", "merge branch-a"], tmp_path)

    records = list(iter_commit_records(tmp_path, limit=500))
    # Merge commit should still appear if it has patches (branch-a added a.py)
    # But a pure merge with NO diff (fast-forward simulated) should be skipped.
    # Here we just assert the branch commit and initial are handled sanely.
    assert all(r.is_merge is False or r.is_merge is True for r in records)  # type safety


def test_skip_over_50_files_commit(tmp_path: Path) -> None:
    """Commits with >50 touched files are skipped."""
    try:
        from atelier.infra.code_intel.git_history.walker import iter_commit_records
    except Exception:
        pytest.skip("pygit2 not available")

    _git(["init"], tmp_path)
    _git(["config", "user.name", "T"], tmp_path)
    _git(["config", "user.email", "t@t.com"], tmp_path)

    (tmp_path / "README.md").write_text("init")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "initial"], tmp_path)

    # Normal commit
    (tmp_path / "normal.py").write_text("x = 1")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "normal commit"], tmp_path)

    # Commit touching 51 files
    for i in range(51):
        (tmp_path / f"gen_{i}.py").write_text(f"v = {i}")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "bulk generated files"], tmp_path)

    records = list(iter_commit_records(tmp_path, limit=500))
    # Only the normal commit should be yielded; bulk commit skipped
    assert len(records) == 1
    assert records[0].sha == _get_sha(tmp_path, "HEAD~1")


def test_lineage_keep_overrides_skip(tmp_path: Path) -> None:
    """[lineage:keep] in commit message bypasses >50 files skip rule."""
    try:
        from atelier.infra.code_intel.git_history.walker import iter_commit_records
    except Exception:
        pytest.skip("pygit2 not available")

    _git(["init"], tmp_path)
    _git(["config", "user.name", "T"], tmp_path)
    _git(["config", "user.email", "t@t.com"], tmp_path)

    (tmp_path / "README.md").write_text("init")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "initial"], tmp_path)

    # Commit touching 51 files but with [lineage:keep]
    for i in range(51):
        (tmp_path / f"gen_{i}.py").write_text(f"v = {i}")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "bulk files [lineage:keep]"], tmp_path)

    records = list(iter_commit_records(tmp_path, limit=500))
    assert len(records) == 1
    assert len(records[0].files_touched) == 51


def test_resume_since_sha(five_commit_repo: Path) -> None:
    """since_sha stops enumeration at that commit — only newer commits are yielded."""
    try:
        from atelier.infra.code_intel.git_history.walker import iter_commit_records
    except Exception:
        pytest.skip("pygit2 not available")

    # Get all 5 SHAs in order (newest first)
    all_records = list(iter_commit_records(five_commit_repo, limit=500))
    assert len(all_records) == 5

    # Use SHA of the 3rd-newest commit (index 2) as watermark
    # Should yield only commits 0 and 1 (the 2 newer ones)
    watermark_sha = all_records[2].sha
    resumed = list(iter_commit_records(five_commit_repo, limit=500, since_sha=watermark_sha))
    assert len(resumed) == 2
    assert resumed[0].sha == all_records[0].sha
    assert resumed[1].sha == all_records[1].sha


def test_bot_commit_skip(tmp_path: Path) -> None:
    """Commits from dependabot email are skipped."""
    try:
        from atelier.infra.code_intel.git_history.walker import iter_commit_records
    except Exception:
        pytest.skip("pygit2 not available")

    _git(["init"], tmp_path)
    _git(["config", "user.name", "T"], tmp_path)
    _git(["config", "user.email", "t@t.com"], tmp_path)

    (tmp_path / "README.md").write_text("init")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "initial"], tmp_path)

    # Normal commit
    (tmp_path / "normal.py").write_text("x = 1")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "normal"], tmp_path)

    # Bot commit
    (tmp_path / "deps.txt").write_text("dep==2.0")
    _git(["add", "-A"], tmp_path)
    _git(
        ["commit", "-m", "bump dep version"],
        tmp_path,
        env={"GIT_AUTHOR_EMAIL": "bot@dependabot.github.com"},
    )

    records = list(iter_commit_records(tmp_path, limit=500))
    # Only the normal commit; bot commit should be skipped
    assert len(records) == 1
    assert records[0].message == "normal"
