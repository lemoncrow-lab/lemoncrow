"""Round-trip integration tests for batch_edit (WP-22).

Tests that the ``apply_batch_edit`` core helper (shared by the MCP ``edit``
tool handler) produces correct results end-to-end, and that the JSON schema
is stable.
"""

from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.tool_supervision.batch_edit import apply_batch_edit

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------- #
# JSON schema / envelope                                                      #
# --------------------------------------------------------------------------- #


def test_result_envelope_keys(tmp_path: Path) -> None:
    """Result always has applied, failed, rolled_back keys."""
    f = tmp_path / "a.txt"
    _write(f, "hello\n")

    result = apply_batch_edit(
        [{"path": str(f), "op": "replace", "old_string": "hello", "new_string": "world"}],
        atomic=True,
        repo_root=tmp_path,
    )

    assert set(result.keys()) >= {"applied", "failed", "rolled_back"}


def test_applied_hunk_structure(tmp_path: Path) -> None:
    """Each applied entry has path + hunks with line_start / line_end."""
    f = tmp_path / "b.txt"
    _write(f, "aaa\nbbb\nccc\n")

    result = apply_batch_edit(
        [{"path": str(f), "op": "replace", "old_string": "bbb", "new_string": "BBB"}],
        atomic=True,
        repo_root=tmp_path,
    )

    assert len(result["applied"]) == 1
    hunk = result["applied"][0]["hunks"][0]
    assert "line_start" in hunk
    assert "line_end" in hunk
    assert isinstance(hunk["line_start"], int)


def test_replace_trailing_newline_line_end_inclusive(tmp_path: Path) -> None:
    """A multi-line replace whose old_string ends with a newline reports the
    inclusive last replaced line, not the line after the block (WP-22 off-by-one)."""
    f = tmp_path / "c.txt"
    _write(f, "line1\nline2\nline3\n")

    result = apply_batch_edit(
        [{"path": str(f), "op": "replace", "old_string": "line2\nline3\n", "new_string": "X\n"}],
        atomic=True,
        repo_root=tmp_path,
    )

    hunk = result["applied"][0]["hunks"][0]
    assert hunk["line_start"] == 2
    assert hunk["line_end"] == 3  # spans lines 2-3 inclusive, not 4 (past EOF)


def test_replace_single_line_trailing_newline_line_end(tmp_path: Path) -> None:
    """A single-line replace ending with a newline reports line_end == line_start."""
    f = tmp_path / "d.txt"
    _write(f, "line1\nline2\nline3\n")

    result = apply_batch_edit(
        [{"path": str(f), "op": "replace", "old_string": "line2\n", "new_string": "X\n"}],
        atomic=True,
        repo_root=tmp_path,
    )

    hunk = result["applied"][0]["hunks"][0]
    assert hunk["line_start"] == 2
    assert hunk["line_end"] == 2


# --------------------------------------------------------------------------- #
# Idempotency and backup cleanup                                             #
# --------------------------------------------------------------------------- #


def test_backup_cleaned_up_on_success(tmp_path: Path) -> None:
    """Backup directory is removed after a successful atomic batch."""
    f = tmp_path / "f.txt"
    _write(f, "data\n")

    apply_batch_edit(
        [{"path": str(f), "op": "replace", "old_string": "data", "new_string": "new data"}],
        atomic=True,
        backup_base=tmp_path / ".lemoncrow" / "run" / "test-run" / "batch_edit_backup",
        repo_root=tmp_path,
    )

    backup_dir = tmp_path / ".lemoncrow" / "run" / "test-run" / "batch_edit_backup"
    assert not backup_dir.exists(), "backup dir should be removed after success"


# --------------------------------------------------------------------------- #
# Host-native docs note                                                      #
# --------------------------------------------------------------------------- #


def test_batch_edit_module_docstring_mentions_host_native() -> None:
    """The module docstring must state that host-native edit tools remain the default."""
    from lemoncrow.core.capabilities.tool_supervision import batch_edit

    doc = batch_edit.__doc__ or ""
    assert (
        "host" in doc.lower() or "native" in doc.lower()
    ), "batch_edit module docstring should reference host-native edit tools"
