"""The one footer grammar for every shrink, spill, truncate or compact event.

A model that reads LemonCrow output should learn exactly one pattern for "this
result was cut, and here is where the rest is", whichever surface produced it.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["spill_notice"]


def spill_notice(
    *,
    verb: str,
    original_chars: int,
    kept_chars: int,
    path: Path | str | None = None,
) -> str:
    """The canonical footer for a shrink/spill/truncate/compact event.

    Counts are CHARACTERS, never bytes. ``path`` names the file the full content
    can be recovered from with ``read <path>``. Pass ``None`` when there is no
    recovery path; the notice then reports a hard truncation with no ``read``
    hint (``verb`` is unused in that shape: without a recovery path the event is,
    from the model's side, just a truncation).
    """
    if path is None:
        return f"[lc: truncated {original_chars}→{kept_chars}; narrow the query for full]"
    return f"[lc: {verb} {original_chars}→{kept_chars}; full: {path}]"
