"""A Snapshot's packet, indexed so reading one file of it costs that file and not the whole Review.

A Reader request for one file's patch or contents used to gunzip and decode the whole packet (10 MiB of JSON for
a 10,000-file change, about 70 ms of CPU under the GIL) only to look one file up. A packet is immutable for a
``packet_sha256``, so the Reader decodes it once into this index and every later request decodes a single
per-file JSON blob.

What an index holds in memory: one compact JSON blob per changed file and one per file that has symbols (about the
size of the packet's own ``files`` and ``symbols`` text, 10 MiB for the 10,000-file fixture where the decoded dicts
would take 28 MiB), plus the packet's provenance record and evidence rows as decoded data (a few hundred KiB).
``nbytes`` is the size of the blobs and their paths, the number the store bounds its memo by.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Mapping
from typing import Any


def decode_packet_bytes(raw: bytes | None) -> dict[str, Any] | None:
    """The packet a Snapshot stored, as plain data, or ``None`` when the bytes are absent or not a packet."""

    if raw is None:
        return None
    try:
        decoded = json.loads(gzip.decompress(raw).decode("utf-8"))
    except (OSError, EOFError, UnicodeDecodeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _blob(value: Any) -> bytes:
    # ASCII-escaped so a lone surrogate in a path or a patch survives the round trip.
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")


class PacketIndex:
    """Read-only view of one decoded packet.

    ``file`` and ``symbols`` return fresh data the caller may keep or change. ``provenance`` and ``evidence`` are
    shared by every reader of this Snapshot, so callers read or copy them and never change them in place.
    """

    __slots__ = ("_files", "_symbols", "evidence", "nbytes", "provenance")

    def __init__(self, packet: Mapping[str, Any]) -> None:
        files: dict[str, bytes] = {}
        # The same rule as the Reader's file lookup: dict entries only, and a later entry for a path wins.
        for item in packet.get("files") or ():
            if isinstance(item, dict):
                files[str(item.get("path") or "")] = _blob(item)
        by_path: dict[str, list[Any]] = {}
        for symbol in packet.get("symbols") or ():
            if isinstance(symbol, dict):
                by_path.setdefault(str(symbol.get("file_path") or ""), []).append(symbol)
        self._files = files
        self._symbols = {path: _blob(rows) for path, rows in by_path.items()}
        record = packet.get("provenance")
        self.provenance: Mapping[str, Any] = record if isinstance(record, dict) else {}
        self.evidence: Any = packet.get("evidence")
        self.nbytes = sum(len(path) + len(blob) for path, blob in files.items()) + sum(
            len(path) + len(blob) for path, blob in self._symbols.items()
        )

    def file(self, path: str) -> dict[str, Any] | None:
        """The packet's entry for *path*, or ``None`` when the packet has none."""

        blob = self._files.get(path)
        if blob is None:
            return None
        entry: dict[str, Any] = json.loads(blob)
        return entry

    def slice_for(self, path: str, entry: Mapping[str, Any]) -> dict[str, Any]:
        """The packet reduced to one file, in the packet's own shape, for helpers that read a packet by path."""

        return {
            "files": [entry],
            "symbols": self.symbols(path),
            "provenance": self.provenance,
            "evidence": self.evidence,
        }

    def symbols(self, path: str) -> list[dict[str, Any]]:
        """The packet's symbol records of *path*, in packet order."""

        blob = self._symbols.get(path)
        if blob is None:
            return []
        rows: list[dict[str, Any]] = json.loads(blob)
        return rows


def build_packet_index(raw: bytes | None) -> PacketIndex | None:
    """Index the stored packet bytes, or ``None`` when they are not a readable packet."""

    packet = decode_packet_bytes(raw)
    return None if packet is None else PacketIndex(packet)
