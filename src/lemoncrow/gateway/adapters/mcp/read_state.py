"""Session-scoped read freshness state shared by read and edit handlers."""

from __future__ import annotations

import contextlib
import struct
import threading
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import Any

from lemoncrow.gateway.adapters.mcp.ledger import _MAX_HTTP_SESSION_LEDGERS, _request_ledger
from lemoncrow.infra.runtime.run_ledger import RunLedger

RANGE_READ_SIGS: OrderedDict[str, dict[str, tuple[int, int, bool, bytes]]] = OrderedDict()
range_read_sigs_lock = threading.Lock()
MAX_RANGE_READ_SIG_SESSIONS = _MAX_HTTP_SESSION_LEDGERS

LINE_DIGEST = struct.Struct("<II")
DIGEST_WIDTH = LINE_DIGEST.size
MAX_SIG_DIGEST_BYTES = 4_000_000
RELOCATE_CONTEXT_RADII = (3, 1)
MAX_RANGE_READ_SIG_PATHS = 512


def range_read_sigs() -> dict[str, tuple[int, int, bool, bytes]]:
    """Return the current MCP session's freshness-signature bucket."""
    ledger = getattr(_request_ledger, "value", None)
    session_id = ledger.session_id if isinstance(ledger, RunLedger) and ledger.session_id else "_global"
    with range_read_sigs_lock:
        bucket = RANGE_READ_SIGS.get(session_id)
        if bucket is None:
            if len(RANGE_READ_SIGS) >= MAX_RANGE_READ_SIG_SESSIONS:
                RANGE_READ_SIGS.popitem(last=False)
            bucket = {}
            RANGE_READ_SIGS[session_id] = bucket
        else:
            RANGE_READ_SIGS.move_to_end(session_id)
        return bucket


def line_digests(text: str) -> bytes:
    """Return one fixed-width crc32+length fingerprint per logical line."""
    pack = LINE_DIGEST.pack
    crc = zlib.crc32
    out = bytearray()
    for line in text.splitlines():
        raw = line.encode("utf-8", "surrogatepass")
        out += pack(crc(raw), len(raw) & 0xFFFF_FFFF)
    return bytes(out)


def aligned_occurrences(haystack: bytes, needle: bytes, limit: int = 8) -> list[int]:
    """Return line indices where a digest run occurs at slot boundaries."""
    hits: list[int] = []
    pos = 0
    while len(hits) < limit:
        found = haystack.find(needle, pos)
        if found < 0:
            break
        if found % DIGEST_WIDTH == 0:
            hits.append(found // DIGEST_WIDTH)
            pos = found + DIGEST_WIDTH
        else:
            pos = found + 1
    return hits


def relocate_served_range(served: bytes, current: bytes, start_line: int, end_line: int) -> tuple[int, int] | None:
    """Locate a previously served exact line block in the current file safely."""
    line_count = len(served) // DIGEST_WIDTH
    start_index = start_line - 1
    end_index = end_line
    if start_index < 0 or end_index > line_count or start_index >= end_index:
        return None
    block = served[start_index * DIGEST_WIDTH : end_index * DIGEST_WIDTH]
    if current[start_index * DIGEST_WIDTH : end_index * DIGEST_WIDTH] == block:
        return (start_line, end_line)
    for radius in RELOCATE_CONTEXT_RADII:
        low = max(0, start_index - radius)
        high = min(line_count, end_index + radius)
        if low == start_index and high == end_index:
            continue
        hits = aligned_occurrences(current, served[low * DIGEST_WIDTH : high * DIGEST_WIDTH])
        if len(hits) == 1:
            new_start = hits[0] + (start_index - low) + 1
            return (new_start, new_start + (end_index - start_index) - 1)
    return None


def retarget_range_edit(edit: dict[str, Any], path: str, start: int, end: int) -> None:
    """Repoint an edit at the relocated exact line range, preserving its path key."""
    suffix = f":L{start}" if start == end else f":L{start}-L{end}"
    edit["file_path" if "file_path" in edit else "path"] = path + suffix


def record_read_sig(path: Path | str, *, exact: bool = True) -> None:
    """Record disk freshness + optional per-line digests for a served file."""
    try:
        resolved = Path(path).resolve()
        stat = resolved.stat()
        digests = b""
        if exact and stat.st_size <= MAX_SIG_DIGEST_BYTES:
            with contextlib.suppress(OSError):
                digests = line_digests(resolved.read_text(encoding="utf-8", errors="replace"))
        bucket = range_read_sigs()
        bucket.pop(str(resolved), None)
        bucket[str(resolved)] = (stat.st_mtime_ns, stat.st_size, exact, digests)
        while len(bucket) > MAX_RANGE_READ_SIG_PATHS:
            bucket.pop(next(iter(bucket)), None)
    except OSError:
        pass


__all__ = [
    "DIGEST_WIDTH",
    "LINE_DIGEST",
    "MAX_RANGE_READ_SIG_PATHS",
    "MAX_RANGE_READ_SIG_SESSIONS",
    "MAX_SIG_DIGEST_BYTES",
    "RANGE_READ_SIGS",
    "RELOCATE_CONTEXT_RADII",
    "aligned_occurrences",
    "line_digests",
    "range_read_sigs",
    "range_read_sigs_lock",
    "record_read_sig",
    "relocate_served_range",
    "retarget_range_edit",
]
