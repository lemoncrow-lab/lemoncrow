"""Source-only section extraction for consolidated persona partials."""

from __future__ import annotations

from pathlib import Path

_SECTION_END = "<!-- lc:end -->"


def markdown_section(path: Path, name: str) -> str:
    """Return one ``<!-- lc:section NAME -->`` block without source markers."""
    source = path.read_text(encoding="utf-8")
    start = f"<!-- lc:section {name} -->"
    count = source.count(start)
    if count != 1:
        raise ValueError(f"{path}: expected exactly one {start!r}, found {count}")
    tail = source.partition(start)[2]
    body, end, _remainder = tail.partition(_SECTION_END)
    if not end:
        raise ValueError(f"{path}: section {name!r} has no {_SECTION_END!r}")
    return body.strip()


__all__ = ["markdown_section"]
