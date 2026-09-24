"""The read tool's path grammar: ``path:L10-L20``, ``:full``, ``:head=N`` and the rest.

One parser for the main package's ``read`` and the thin client's offline
``read_from_disk``.
"""

from __future__ import annotations

import re

READ_RANGE_SUFFIX = re.compile(r":L?(\d+)(?:-L?(\d+))?$", re.IGNORECASE)
RANGE_TOKEN_RE = re.compile(r"^L?\d+(-L?\d*)?$", re.IGNORECASE)


def split_read_range_suffix(raw_path: str) -> tuple[str, str | None]:
    """Split a trailing ``:Lx`` / ``:Lx-Ly`` suffix from a read path."""
    match = READ_RANGE_SUFFIX.search(raw_path)
    if match is None:
        return raw_path, None
    start = match.group(1)
    end = match.group(2)
    range_spec = f"L{start}-L{end}" if end else f"L{start}"
    return raw_path[: match.start()], range_spec


def split_file_opts(raw: str) -> tuple[str, str | None, bool, int | None, int | None, bool, bool]:
    """Parse the public read tool's colon-suffixed path options."""
    expand = False
    line_range: str | None = None
    head: int | None = None
    tail: int | None = None
    summary = False
    outline = False
    parts = raw.split(":")
    while len(parts) > 1:
        token = parts[-1]
        if token in ("full", "full=true", "full=1"):
            expand = True
            parts.pop()
        elif token in ("summary", "summary=true", "summary=1"):
            summary = True
            parts.pop()
        elif token in ("outline", "outline=true", "outline=1"):
            outline = True
            parts.pop()
        elif token.startswith("head="):
            try:
                head = int(token[5:])
                parts.pop()
            except ValueError:
                break
        elif token.startswith("tail="):
            try:
                tail = int(token[5:])
                parts.pop()
            except ValueError:
                break
        elif RANGE_TOKEN_RE.match(token):
            line_range = token
            parts.pop()
        else:
            break
    return ":".join(parts), line_range, expand, head, tail, summary, outline


__all__ = ["RANGE_TOKEN_RE", "READ_RANGE_SUFFIX", "split_file_opts", "split_read_range_suffix"]
