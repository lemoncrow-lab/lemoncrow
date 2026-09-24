from __future__ import annotations

from pathlib import Path
from typing import Any

import pygit2

from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range, source_state


def _repo(root: Path) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.invalid"
    return repo


def _write(root: Path, text: str) -> None:
    (root / "app.py").write_text(text, encoding="utf-8")


def _commit(repo: Any, message: str) -> None:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    sig = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", sig, sig, message, tree, parents)


def test_working_tree_source_state_is_content_sensitive_and_reversible(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    repo = _repo(root)
    _write(root, "value = 1\n")
    _commit(repo, "base")

    _write(root, "value = 2\n")
    rng = resolve_rev_range(root, working_tree=True)
    first = source_state(root, rng)
    assert first.paths == ("app.py",)

    # Same path/status/size, different bytes: detection must still move.
    _write(root, "value = 3\n")
    second = source_state(root, rng)
    assert second.fingerprint != first.fingerprint

    _write(root, "value = 2\n")
    assert source_state(root, rng).fingerprint == first.fingerprint


def test_staged_source_state_ignores_unstaged_edits_until_the_index_moves(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    repo = _repo(root)
    _write(root, "value = 1\n")
    _commit(repo, "base")

    _write(root, "value = 2\n")
    repo.index.add("app.py")
    repo.index.write()
    rng = resolve_rev_range(root, staged=True)
    staged = source_state(root, rng)

    _write(root, "value = 3\n")
    assert source_state(root, rng).fingerprint == staged.fingerprint

    repo.index.add("app.py")
    repo.index.write()
    assert source_state(root, rng).fingerprint != staged.fingerprint


def test_working_tree_source_state_moves_when_head_moves_under_same_file_bytes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    repo = _repo(root)
    _write(root, "value = 1\n")
    _commit(repo, "base")

    _write(root, "value = 2\n")
    first_range = resolve_rev_range(root, working_tree=True)
    first = source_state(root, first_range)

    # Commit an unrelated file while leaving app.py's working-tree bytes alone.
    # The visible app.py patch is now relative to a different HEAD and therefore
    # must not retain the old source-state identity.
    (root / "meta.txt").write_text("meta\n", encoding="utf-8")
    repo.index.add("meta.txt")
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000001, 0)
    repo.create_commit("HEAD", sig, sig, "move base", tree, [repo.head.target])

    second_range = resolve_rev_range(root, working_tree=True)
    second = source_state(root, second_range)
    assert second_range.base_sha != first_range.base_sha
    assert second.paths == first.paths == ("app.py",)
    assert second.fingerprint != first.fingerprint
