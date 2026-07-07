"""Tests for batch_edit atomicity guarantees (WP-22)."""

from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.tool_supervision.batch_edit import apply_batch_edit

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Atomic mode                                                                 #
# --------------------------------------------------------------------------- #


def test_atomic_all_succeed(tmp_path: Path) -> None:
    """All edits applied; backup directory cleaned up afterwards."""
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    _write(f1, "hello world\n")
    _write(f2, "foo bar\n")

    result = apply_batch_edit(
        [
            {"path": str(f1), "op": "replace", "old_string": "hello", "new_string": "bye"},
            {"path": str(f2), "op": "replace", "old_string": "foo", "new_string": "baz"},
        ],
        atomic=True,
        repo_root=tmp_path,
    )

    assert result["rolled_back"] is False
    assert len(result["applied"]) == 2
    assert result["failed"] == []
    assert f1.read_text() == "bye world\n"
    assert f2.read_text() == "baz bar\n"

    # Backup directory must not remain after success.
    backup_dirs = list((tmp_path / ".atelier" / "run").glob("*/batch_edit_backup"))
    assert backup_dirs == [], "backup directory should be cleaned up on success"


def test_atomic_one_fail_rolls_back_all(tmp_path: Path) -> None:
    """One failing edit in atomic mode → no files are modified."""
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    _write(f1, "original content\n")
    _write(f2, "another file\n")

    result = apply_batch_edit(
        [
            {
                "path": str(f1),
                "op": "replace",
                "old_string": "original",
                "new_string": "modified",
            },
            {
                "path": str(f2),
                "op": "replace",
                "old_string": "DOES_NOT_EXIST",
                "new_string": "x",
            },
        ],
        atomic=True,
        repo_root=tmp_path,
    )

    assert result["rolled_back"] is True
    assert result["applied"] == []
    assert len(result["failed"]) == 1
    # Both files must be untouched.
    assert f1.read_text() == "original content\n"
    assert f2.read_text() == "another file\n"


def test_atomic_first_edit_rolled_back_when_second_fails(tmp_path: Path) -> None:
    """First edit applied, second fails → first edit is reverted."""
    f1 = tmp_path / "x.py"
    f2 = tmp_path / "y.py"
    _write(f1, "def foo(): pass\n")
    _write(f2, "def bar(): pass\n")

    result = apply_batch_edit(
        [
            {
                "path": str(f1),
                "op": "replace",
                "old_string": "def foo(): pass",
                "new_string": "def foo(): return 1",
            },
            {
                "path": str(f2),
                "op": "replace",
                "old_string": "MISSING_STRING",
                "new_string": "x",
            },
        ],
        atomic=True,
        repo_root=tmp_path,
    )

    assert result["rolled_back"] is True
    # Original content restored.
    assert f1.read_text() == "def foo(): pass\n"
    assert f2.read_text() == "def bar(): pass\n"


# --------------------------------------------------------------------------- #
# Non-atomic mode                                                             #
# --------------------------------------------------------------------------- #


def test_non_atomic_partial_success(tmp_path: Path) -> None:
    """In non-atomic mode, successful edits persist even if one fails."""
    f1 = tmp_path / "ok.txt"
    f2 = tmp_path / "fail.txt"
    _write(f1, "target string\n")
    _write(f2, "something\n")

    result = apply_batch_edit(
        [
            {
                "path": str(f1),
                "op": "replace",
                "old_string": "target string",
                "new_string": "replaced",
            },
            {
                "path": str(f2),
                "op": "replace",
                "old_string": "NOT_PRESENT",
                "new_string": "x",
            },
        ],
        atomic=False,
        repo_root=tmp_path,
    )

    assert result["rolled_back"] is False
    assert len(result["applied"]) == 1
    assert len(result["failed"]) == 1
    # Successful edit persisted.
    assert f1.read_text() == "replaced\n"
    # File with failing edit unchanged.
    assert f2.read_text() == "something\n"


# --------------------------------------------------------------------------- #
# Safety: path escape                                                         #
# --------------------------------------------------------------------------- #


def test_path_escape_rejected(tmp_path: Path) -> None:
    """Paths outside the repo root are rejected."""
    result = apply_batch_edit(
        [
            {
                "path": "/etc/passwd",
                "op": "replace",
                "old_string": "root",
                "new_string": "hacked",
            }
        ],
        atomic=True,
        repo_root=tmp_path,
    )
    assert len(result["failed"]) == 1
    assert "escape" in result["failed"][0]["error"].lower() or "outside" in result["failed"][0]["error"].lower()


# --------------------------------------------------------------------------- #
# Op variants                                                                 #
# --------------------------------------------------------------------------- #


def test_insert_after_op(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    _write(f, "def baz():\n    pass\n")

    result = apply_batch_edit(
        [
            {
                "path": str(f),
                "op": "insert_after",
                "anchor": "def baz",
                "new_string": "    return 42",
            }
        ],
        atomic=True,
        repo_root=tmp_path,
    )

    assert result["applied"], result
    assert "return 42" in f.read_text()


def test_replace_range_op(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    _write(f, "line1\nline2\nline3\nline4\n")

    result = apply_batch_edit(
        [
            {
                "path": str(f),
                "op": "replace_range",
                "line_start": 2,
                "line_end": 3,
                "new_string": "REPLACED",
            }
        ],
        atomic=True,
        repo_root=tmp_path,
    )

    assert result["applied"], result
    lines = f.read_text().splitlines()
    assert lines[1] == "REPLACED"
    assert lines[0] == "line1"
    assert lines[2] == "line4"


def test_unknown_op_reported_as_failure(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    _write(f, "content\n")

    result = apply_batch_edit(
        [{"path": str(f), "op": "teleport", "new_string": "x"}],
        atomic=False,
        repo_root=tmp_path,
    )

    assert len(result["failed"]) == 1
    assert "teleport" in result["failed"][0]["error"]
