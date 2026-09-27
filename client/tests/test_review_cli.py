from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path

from lemoncrow_client.cli import main
from lemoncrow_client.review import _freeze_range, _gitlinks_for_range, _materialize_commit, resolve_review_range


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "review@example.test")
    _git(repo, "config", "user.name", "Review Test")
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "a.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_review_help_is_part_of_the_thin_cli() -> None:
    sink = StringIO()
    assert main(["review", "--help"], stdout=sink) == 0
    assert "--working-tree" in sink.getvalue()
    assert "--staged" in sink.getvalue()


def test_dirty_default_resolves_to_working_tree_without_pygit2(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "new.py").write_text("new = True\n", encoding="utf-8")

    resolved = resolve_review_range(repo)

    assert resolved.mode == "working_tree"
    assert resolved.base_rev == "HEAD"
    assert resolved.head_rev == "WORKDIR"
    assert resolved.dirty is True


def test_working_tree_snapshot_is_exact_and_excludes_ignored_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "new.py").write_text("new = True\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("secret cache\n", encoding="utf-8")
    resolved = resolve_review_range(repo, working_tree=True)
    base, head = tmp_path / "base", tmp_path / "head"

    _freeze_range(repo, resolved, base, head)

    assert (base / "a.py").read_text() == "value = 1\n"
    assert (head / "a.py").read_text() == "value = 2\n"
    assert (head / "new.py").read_text() == "new = True\n"
    assert not (head / "ignored.txt").exists()
    assert not (head / ".git").exists()


def test_staged_snapshot_does_not_include_unstaged_edit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    (repo / "a.py").write_text("value = 3\n", encoding="utf-8")
    resolved = resolve_review_range(repo, staged=True)
    base, head = tmp_path / "base", tmp_path / "head"

    _freeze_range(repo, resolved, base, head)

    assert (base / "a.py").read_text() == "value = 1\n"
    assert (head / "a.py").read_text() == "value = 2\n"


def test_materialized_commit_is_the_exact_tree_and_leaves_the_index_alone(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".gitattributes").write_text("hidden.txt export-ignore\n", encoding="utf-8")
    (repo / "hidden.txt").write_text("kept\n", encoding="utf-8")
    (repo / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "run.sh").chmod(0o755)
    (repo / "link.py").symlink_to("a.py")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "shapes")
    sha = _git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("value = 99\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    staged_before = _git(repo, "diff", "--cached", "--name-only")
    target = tmp_path / "frozen"

    _materialize_commit(repo, sha, target)

    assert (target / "a.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (target / "hidden.txt").read_text(encoding="utf-8") == "kept\n"
    assert (target / "run.sh").stat().st_mode & 0o111
    assert (target / "link.py").is_symlink()
    assert (target / "link.py").readlink() == Path("a.py")
    assert _git(repo, "diff", "--cached", "--name-only") == staged_before == "a.py"


def test_clean_default_resolves_previous_commit_range(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "second")

    resolved = resolve_review_range(repo)

    assert resolved.mode == "commit_range"
    assert resolved.base_rev == "HEAD~1"
    assert resolved.head_rev == "HEAD"
    assert resolved.base_sha == _git(repo, "rev-parse", "HEAD~1")
    assert resolved.head_sha == _git(repo, "rev-parse", "HEAD")


def test_named_branch_range_keeps_a_stable_review_source_ref(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "branch", "base-branch")
    _git(repo, "checkout", "-q", "-b", "feature-branch")
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "feature")

    resolved = resolve_review_range(repo, "base-branch...feature-branch")

    assert resolved.source_ref(repo) == "named:refs/heads/base-branch...refs/heads/feature-branch"


def test_sha_and_relative_ranges_remain_snapshot_keyed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "second")
    resolved = resolve_review_range(repo, "HEAD~1..HEAD")

    assert resolved.source_ref(repo) == f"{resolved.base_sha}..{resolved.head_sha}"


def test_working_tree_snapshot_preserves_a_tracked_deletion(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").unlink()
    resolved = resolve_review_range(repo, working_tree=True)
    base, head = tmp_path / "base", tmp_path / "head"

    _freeze_range(repo, resolved, base, head)

    assert (base / "a.py").is_file()
    assert not (head / "a.py").exists()


def test_working_tree_gitlink_pointer_move_is_preserved(tmp_path: Path) -> None:
    sub = tmp_path / "sub-source"
    sub.mkdir()
    _git(sub, "init", "-q")
    _git(sub, "config", "user.email", "review@example.test")
    _git(sub, "config", "user.name", "Review Test")
    (sub / "value.txt").write_text("one\n", encoding="utf-8")
    _git(sub, "add", ".")
    _git(sub, "commit", "-q", "-m", "one")
    first = _git(sub, "rev-parse", "HEAD")
    (sub / "value.txt").write_text("two\n", encoding="utf-8")
    _git(sub, "commit", "-qam", "two")
    second = _git(sub, "rev-parse", "HEAD")
    _git(sub, "checkout", "-q", first)

    repo = _repo(tmp_path)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "deps/sub")
    _git(repo, "commit", "-qam", "add submodule")
    _git(repo / "deps/sub", "checkout", "-q", second)

    resolved = resolve_review_range(repo, working_tree=True)
    base, head = _gitlinks_for_range(repo, resolved)

    assert base == {"deps/sub": first}
    assert head == {"deps/sub": second}
