"""kit's file I/O: the one kit module allowed to write."""

from __future__ import annotations

from pathlib import Path

import pytest
from lemoncrow_client.kit.fsio import FileSnapshot, atomic_write, read_text, restore, restore_bytes, snapshot


def test_atomic_write_keeps_the_mode_and_leaves_no_litter(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_text("old\n", encoding="utf-8")
    script.chmod(0o755)

    atomic_write(script, "new\n")

    assert script.read_text(encoding="utf-8") == "new\n"
    assert script.stat().st_mode & 0o777 == 0o755
    assert [path.name for path in tmp_path.iterdir()] == ["run.sh"]


def test_atomic_write_creates_missing_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "er" / "file.txt"
    atomic_write(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_read_text_refuses_oversized_and_non_utf8_files(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_text("x" * 11, encoding="utf-8")
    with pytest.raises(ValueError, match=r"exceeds 10 bytes: big\.txt"):
        read_text(big, max_bytes=10)

    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ValueError, match=r"not UTF-8 text: blob\.bin"):
        read_text(blob)


def test_snapshot_and_restore_round_trip(tmp_path: Path) -> None:
    existing = tmp_path / "a.txt"
    existing.write_text("before\n", encoding="utf-8")
    created = tmp_path / "b.txt"

    snaps = snapshot({"a.txt": existing, "b.txt": created})
    assert snaps["a.txt"] == FileSnapshot(existing, True, "before\n")
    assert snaps["b.txt"] == FileSnapshot(created, False, None)

    existing.write_text("after\n", encoding="utf-8")
    created.write_text("new\n", encoding="utf-8")

    assert restore(snaps) == []
    assert existing.read_text(encoding="utf-8") == "before\n"
    assert not created.exists()


def test_restore_leaves_a_file_someone_else_changed(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("before\n", encoding="utf-8")
    snaps = snapshot({"a.txt": target})
    target.write_text("concurrent\n", encoding="utf-8")

    assert restore(snaps, applied_content={"a.txt": "ours\n"}) == ["a.txt"]
    assert target.read_text(encoding="utf-8") == "concurrent\n"


def test_restore_never_deletes_a_file_that_was_unreadable(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\xff\xfe")
    snaps = snapshot({"blob.bin": target})
    assert snaps["blob.bin"] == FileSnapshot(target, True, None)

    assert restore(snaps) == ["blob.bin"]
    assert target.read_bytes() == b"\xff\xfe"


def test_restore_bytes_rewrites_or_deletes(tmp_path: Path) -> None:
    target = tmp_path / "a.bin"
    target.write_bytes(b"changed")
    restore_bytes(target, b"original")
    assert target.read_bytes() == b"original"

    restore_bytes(target, None)
    assert not target.exists()
    restore_bytes(target, None)  # already gone: still fine
