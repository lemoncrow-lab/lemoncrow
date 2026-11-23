"""The title a Review gets when nobody named it: what changed, never which revision.

The inbox row and the Reader header both read this, so a Review carries one name everywhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Titles the capture gives a Review before anything better is known.
GENERIC_TITLES = frozenset({"working tree", "staged changes"})

#: Up to this many changed definitions are named in the title; more reads as an area summary.
MAX_NAMED_SYMBOLS = 3


def is_generic_title(title: object) -> bool:
    text = str(title or "").strip().lower()
    return not text or text in GENERIC_TITLES


def symbol_title(symbols: Any) -> str:
    """``Change a, b and c`` when at most three definitions changed; empty otherwise."""

    if not isinstance(symbols, list):
        return ""
    names: list[str] = []
    changes: set[str] = set()
    for item in symbols:
        name = str(item.get("symbol_name") or "").strip() if isinstance(item, Mapping) else ""
        if not name:
            continue
        if name not in names:
            names.append(name)
        changes.add(str(item.get("change") or ""))
    if not names or len(names) > MAX_NAMED_SYMBOLS:
        return ""
    verb = {"added": "Add", "deleted": "Remove"}.get(next(iter(changes)), "Change") if len(changes) == 1 else "Change"
    listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return f"{verb} {listed}"


def packet_title(packet: Mapping[str, Any] | None, *, fallback: str = "Changes") -> str:
    """A title from the stored packet: its own, else the changed symbols, else the files' area."""

    own = str((packet or {}).get("title") or "").strip()
    if own and not is_generic_title(own):
        return own
    files = (packet or {}).get("files")
    if not isinstance(files, list) or not files:
        return own or fallback
    paths = [str(item.get("path") or "") for item in files if isinstance(item, Mapping) and item.get("path")]
    if not paths:
        return own or fallback
    named = symbol_title((packet or {}).get("symbols"))
    if named:
        return named
    if len(paths) == 1:
        item = files[0] if isinstance(files[0], Mapping) else {}
        action = {"added": "Add", "deleted": "Remove", "renamed": "Rename"}.get(str(item.get("status")), "Update")
        return f"{action} {paths[0]}"
    areas: dict[str, int] = {}
    for path in paths:
        area = path.split("/", 1)[0] if "/" in path else "root"
        areas[area] = areas.get(area, 0) + 1
    leaders = sorted(areas, key=lambda area: (-areas[area], area))[:2]
    return f"{' + '.join(leaders)}: {len(paths)} files"
