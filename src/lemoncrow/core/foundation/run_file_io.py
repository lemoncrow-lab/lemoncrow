"""Cross-process-safe JSON updates for per-session ``run.json`` files.

Every host hook and :class:`RunLedger` can write the same file from a different
process. Atomic rename prevents torn JSON, but it does not prevent the classic
read/modify/write lost-update race. Writers therefore take one advisory lock
next to the run file for the entire read -> merge -> replace critical section.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any


class RunFileLock(AbstractContextManager["RunFileLock"]):
    """Exclusive advisory lock shared by every ``run.json`` writer.

    POSIX uses ``flock``; Windows uses ``msvcrt.locking`` on the first byte of a
    persistent sidecar. Both APIs block until the short JSON critical section is
    available. The sidecar intentionally remains on disk: unlinking a lock file
    while another process still has it open can create two independent locks.
    """

    def __init__(self, run_file: Path) -> None:
        self.path = run_file.with_name(f".{run_file.name}.lock")
        self._handle: Any = None

    def __enter__(self) -> RunFileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        self._handle = handle
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            locking = getattr(msvcrt, "locking")
            locking(handle.fileno(), getattr(msvcrt, "LK_LOCK"), 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                locking = getattr(msvcrt, "locking")
                locking(handle.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace *path* with *payload*.

    Callers that participate in a read/modify/write cycle must hold
    :class:`RunFileLock` across both the read and this write.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
