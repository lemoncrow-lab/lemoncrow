"""Storage ID helpers."""

from __future__ import annotations

import os
import secrets
import threading
import time

_lock = threading.Lock()
_last_ms = 0
_last_counter = 0


def _reset_after_fork() -> None:
    """A forked child inherits a copy of ``_lock`` in whatever state another thread left it, and nothing there will
    ever release it; the counter is also the parent's. Start the child from a fresh lock and a fresh counter."""

    global _lock, _last_ms, _last_counter
    _lock = threading.Lock()
    _last_ms = 0
    _last_counter = 0


if hasattr(os, "register_at_fork"):  # POSIX only
    os.register_at_fork(after_in_child=_reset_after_fork)


def make_uuid7() -> str:
    """Return a UUIDv7-like sortable identifier using only stdlib.

    The value is formatted as UUID text. Its first 48 bits are the Unix
    timestamp in milliseconds, followed by the UUIDv7 version bits, a 12-bit
    counter (RFC 9562 method 1) and the variant bits plus random data.

    Within one process the text order is the creation order, even inside one
    millisecond: ``ORDER BY created_at, id`` is then a stable tie-break for rows
    written in the same instant. (``lc-debt: ordering across processes is only
    millisecond-accurate; a sequence column is the upgrade path``.)
    """

    global _last_ms, _last_counter
    with _lock:
        unix_ms = int(time.time() * 1000) & ((1 << 48) - 1)
        if unix_ms > _last_ms:
            counter = secrets.randbits(11)  # headroom so one millisecond can hold 2048+ ids
        else:
            unix_ms = _last_ms
            counter = _last_counter + 1
            if counter >= 1 << 12:
                unix_ms += 1
                counter = 0
        _last_ms, _last_counter = unix_ms, counter
    value = (unix_ms << 80) | (0x7 << 76) | (counter << 64) | (0x2 << 62) | secrets.randbits(62)
    hex_value = f"{value:032x}"
    return f"{hex_value[:8]}-{hex_value[8:12]}-{hex_value[12:16]}-{hex_value[16:20]}-{hex_value[20:]}"


__all__ = ["make_uuid7"]
