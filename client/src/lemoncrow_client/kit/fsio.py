"""File reads and writes for kit -- the only kit module that writes files.

The packaging audit keeps a closed list of modules that may write; this module
is on it, so every other kit module stays pure. Every write goes through a
sibling temporary file and a rename: a crash mid-write leaves the original file,
never half of one, and a failed write leaves no temporary file behind.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final, NamedTuple

__all__ = [
    "MAX_TEXT_BYTES",
    "FileSnapshot",
    "atomic_write",
    "read_text",
    "restore",
    "restore_bytes",
    "snapshot",
]

logger = logging.getLogger(__name__)

#: Largest file an edit reads. Beyond this the file is not source a model can
#: usefully edit in place, and loading it would only burn memory.
MAX_TEXT_BYTES: Final[int] = 16 * 1024 * 1024

_TEMP_PREFIX: Final[str] = ".lemoncrow-edit-"


class FileSnapshot(NamedTuple):
    """One file's state before an edit, for rollback and comparison."""

    path: Path
    existed: bool
    #: ``None`` when the file was absent, or present but unreadable.
    content: str | None


def read_text(path: Path, *, max_bytes: int = MAX_TEXT_BYTES) -> str:
    """Read a UTF-8 text file, refusing an oversized or non-UTF-8 one."""
    if path.stat().st_size > max_bytes:
        raise ValueError(f"file exceeds {max_bytes} bytes: {path.name}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"file is not UTF-8 text: {path.name}") from exc


def _replace_via_temporary(path: Path, payload: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=_TEMP_PREFIX)
    try:
        if isinstance(payload, bytes):
            with os.fdopen(descriptor, "wb") as binary:
                binary.write(payload)
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as text:
                text.write(payload)
        # A rename replaces the file's mode with mkstemp's 0600; carry the old
        # mode over so scripts and hooks keep their exec bit.
        with contextlib.suppress(FileNotFoundError):
            os.chmod(temporary, path.stat().st_mode & 0o7777)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def atomic_write(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` atomically, keeping its permissions."""
    _replace_via_temporary(path, text)


def restore_bytes(path: Path, payload: bytes | None) -> None:
    """Put back a file's exact earlier bytes; ``None`` means it did not exist."""
    if payload is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    _replace_via_temporary(path, payload)


def snapshot(paths: Mapping[str, Path]) -> dict[str, FileSnapshot]:
    """Record each file's current state, keyed by the caller's display path.

    ``existed`` separates a file that was genuinely absent (restoring deletes it)
    from one that existed but could not be read (restoring must leave it alone,
    because deleting or truncating it would lose data).
    """
    snapshots: dict[str, FileSnapshot] = {}
    for display, path in paths.items():
        existed = path.exists()
        content: str | None = None
        if existed:
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                logger.debug("snapshot could not read %s", path, exc_info=True)
        snapshots[display] = FileSnapshot(path, existed, content)
    return snapshots


def restore(
    snapshots: Mapping[str, FileSnapshot],
    applied_content: Mapping[str, str | None] | None = None,
) -> list[str]:
    """Roll files back to their snapshots; return the display paths left alone.

    With ``applied_content`` (what this caller wrote, per display path), a file
    is restored only while it still holds that content. Otherwise someone else
    changed it after this caller did, and restoring would lose their write. A
    file that existed but was unreadable at snapshot time is also left alone.
    """
    left_alone: list[str] = []
    for display, (path, existed, old_content) in snapshots.items():
        try:
            if applied_content is not None and display in applied_content:
                current = path.read_text(encoding="utf-8") if path.exists() else None
                if current != applied_content[display]:
                    left_alone.append(display)
                    continue
            if not existed:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            elif old_content is None:
                left_alone.append(display)
            else:
                atomic_write(path, old_content)
        except (OSError, UnicodeDecodeError):
            logger.debug("restore failed for %s", path, exc_info=True)
    return left_alone
