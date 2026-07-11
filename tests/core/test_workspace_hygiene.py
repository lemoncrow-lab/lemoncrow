"""Workspace hygiene snapshot and scratch-leftover reporting."""

from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.tool_supervision.workspace_hygiene import (
    scratch_leftovers,
    snapshot_workspace,
)


def test_snapshot_excludes_git_internals(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")
    (tmp_path / "main.py").write_text("print()")
    assert snapshot_workspace(tmp_path) == frozenset({"main.py"})


def test_scratch_leftovers_flags_new_residue_only(tmp_path: Path) -> None:
    (tmp_path / "task.py").write_text("x = 1")
    before = snapshot_workspace(tmp_path)

    (tmp_path / "solution.txt").write_text("answer")  # legitimate artifact
    (tmp_path / "a.out").write_bytes(b"\x7fELF")
    (tmp_path / "debug.log").write_text("trace")
    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "task.cpython-313.pyc").write_bytes(b"\x00")

    leftovers = scratch_leftovers(tmp_path, before)
    assert "a.out" in leftovers
    assert "debug.log" in leftovers
    assert "__pycache__/task.cpython-313.pyc" in leftovers
    assert "solution.txt" not in leftovers
    assert "task.py" not in leftovers


def test_preexisting_residue_is_not_flagged(tmp_path: Path) -> None:
    (tmp_path / "old.log").write_text("old")
    before = snapshot_workspace(tmp_path)
    assert scratch_leftovers(tmp_path, before) == []


def test_missing_root_is_empty(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert snapshot_workspace(missing) == frozenset()
    assert scratch_leftovers(missing, frozenset()) == []
