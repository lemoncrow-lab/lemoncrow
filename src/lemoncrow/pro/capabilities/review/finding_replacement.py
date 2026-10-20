"""A finding's one-click fix: the shape a model may return and the proof that it applies to the Snapshot.

An investigator may attach ``replacement: {start_line, end_line, original, text}`` to a confirmed
finding when the fix is a small, local, certain edit: lines ``start_line..end_line`` of the first
target's file, exactly as ``original`` quotes them, become ``text``. Lines are 1-based, inclusive and
on the new side of the change.

The model's numbers and quote are a claim, and a wrong suggestion is worse than none, so
:func:`verify_replacement` checks the claim against the exact Snapshot head before anything may
render it:

* the line range exists in the file;
* the lines at that range are the ones ``original`` quotes (leading whitespace exact, trailing
  whitespace ignored), so a miscounted range or a file that moved on is caught;
* the range sits inside one right-side hunk of the change, the lines GitHub lets a comment attach to
  (:mod:`github_placement`, the same rule the publisher places comments by);
* the edit changes something and applies cleanly (:func:`change_proposals.replace_line_range`).

A replacement that fails any check is dropped and the finding stays. Pure: no store, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from lemoncrow.pro.capabilities.review.change_proposals import replace_line_range, selected_line_text
from lemoncrow.pro.capabilities.review.github_placement import unplaceable_reason

#: "Small and local": a suggestion longer than this is a rewrite, which a person should review as one.
MAX_LINES: Final[int] = 10
MAX_TEXT: Final[int] = 2000


@dataclass(frozen=True, slots=True)
class Replacement:
    """Lines ``start_line..end_line`` (1-based, inclusive, new side) become ``text``.

    ``verified`` is true only for a replacement :func:`verify_replacement` returned; a parsed claim is
    not, and nothing may post one.
    """

    start_line: int
    end_line: int
    original: str
    text: str
    verified: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {
            "start_line": self.start_line,
            "end_line": self.end_line,
            "original": self.original,
            "text": self.text,
        }


def _count(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_replacement(raw: object) -> Replacement | None:
    """The model's replacement as an unverified claim, or ``None`` when absent or malformed.

    Shape only: a model that returns no replacement, ``null``, or something this cannot read has
    simply not offered one, which is never an error for the finding.
    """

    if not isinstance(raw, Mapping):
        return None
    start, end = _count(raw, "start_line"), _count(raw, "end_line")
    original, text = raw.get("original"), raw.get("text")
    if not isinstance(original, str) or not isinstance(text, str):
        return None
    if not 0 < start <= end or end - start + 1 > MAX_LINES:
        return None
    text = text.rstrip("\n")
    # Text that could close an HTML comment would let a suggestion forge the bot's hidden markers.
    if (
        not original.strip()
        or not text.strip()
        or len(text) > MAX_TEXT
        or "\0" in text
        or "<!--" in text
        or "-->" in text
    ):
        return None
    return Replacement(start, end, original, text)


def hunk_headers(files: object, path: str) -> str:
    """The ``@@ -a,b +c,d @@`` lines of *path* in a stored packet's ``files``, one per hunk.

    What :func:`github_placement.hunk_ranges` reads, without the hunk bodies, which a capture may not
    carry. Empty when the file is not in the packet.
    """

    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        return ""
    for entry in files:
        if isinstance(entry, Mapping) and entry.get("path") == path:
            hunks = entry.get("hunks")
            if not isinstance(hunks, Sequence) or isinstance(hunks, (str, bytes)):
                return ""
            return "\n".join(str(hunk.get("header") or "") for hunk in hunks if isinstance(hunk, Mapping))
    return ""


def _numbered_like_github(source: str) -> bool:
    """Whether ``str.splitlines`` and GitHub count the same lines of *source*.

    ``splitlines`` also breaks on a lone carriage return, a form feed and Unicode separators, which GitHub
    does not; such a file's line numbers cannot be trusted to mean the same line in both, and CRLF files
    are left alone because a suggestion's line endings are not ours to choose.
    """

    if "\r" in source:
        return False
    expected = source.count("\n") + (0 if not source or source.endswith("\n") else 1)
    return len(source.splitlines()) == expected


def _lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines()]


def verify_replacement(replacement: Replacement, *, source: str, hunks: str) -> Replacement | None:
    """*replacement* when it applies to *source* (the file at the exact head) inside a hunk of the change, else ``None``.

    ``hunks`` is the file's hunk headers (:func:`hunk_headers`). Every check is a reason to drop the
    replacement and keep the finding; none is a reason to guess.
    """

    if not _numbered_like_github(source):
        return None
    try:
        current = selected_line_text(source, replacement.start_line, replacement.end_line)
    except ValueError:
        return None
    if _lines(current) != _lines(replacement.original):
        return None
    if unplaceable_reason(
        patch=hunks, anchor_side="new", start_line=replacement.start_line, end_line=replacement.end_line
    ):
        return None
    if _lines(replacement.text) == _lines(current):
        return None
    try:
        applied = replace_line_range(source, replacement.start_line, replacement.end_line, replacement.text)
    except ValueError:
        return None
    if applied == source:
        return None
    return replace(replacement, original=current.rstrip("\n"), verified=True)


__all__ = ["MAX_LINES", "MAX_TEXT", "Replacement", "hunk_headers", "parse_replacement", "verify_replacement"]
