"""Fuzzy matching for the rich-edit path — backed by diff-match-patch."""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from difflib import SequenceMatcher

from diff_match_patch import diff_match_patch as _DMP

# --------------------------------------------------------------------------- #
# Public data types (kept for backward compat)                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FuzzyCandidate:
    start_line: int
    end_line: int
    start_offset: int
    end_offset: int
    distance: int
    ratio: float


class FuzzyAmbiguousMatchError(ValueError):
    """Raised when fuzzy matching finds multiple acceptable candidate ranges."""

    def __init__(self, candidates: list[FuzzyCandidate]) -> None:
        self.candidates = candidates
        ranges = ", ".join(f"{c.start_line}-{c.end_line}" for c in candidates)
        super().__init__(f"fuzzy replace ambiguous candidates at ranges: {ranges}")


# --------------------------------------------------------------------------- #
# Text normalization helpers                                                   #
# --------------------------------------------------------------------------- #

_WS_RUN = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([:;,)\]\}])")


def normalize_for_fuzzy(text: str) -> str:
    """Normalize whitespace to tolerate indentation/trailing differences."""
    lines = text.splitlines()
    normalized_lines = []
    for line in lines:
        expanded = line.expandtabs(8).rstrip()
        collapsed = _WS_RUN.sub(" ", expanded).strip()
        normalized_lines.append(_SPACE_BEFORE_PUNCT.sub(r"\1", collapsed))
    return "\n".join(normalized_lines)


# --------------------------------------------------------------------------- #
# Levenshtein (kept for callers / tests that import it directly)              #
# --------------------------------------------------------------------------- #


def bounded_levenshtein(a: str, b: str, max_distance: int) -> int | None:
    """Return edit distance if <= max_distance, else None."""
    if max_distance < 0:
        return None
    if abs(len(a) - len(b)) > max_distance:
        return None
    if a == b:
        return 0

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        row_min = current[0]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current[j] = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + cost,
            )
            if current[j] < row_min:
                row_min = current[j]
        if row_min > max_distance:
            return None
        previous = current

    distance = previous[-1]
    return distance if distance <= max_distance else None


# --------------------------------------------------------------------------- #
# Core fuzzy replace — diff-match-patch backed                                #
# --------------------------------------------------------------------------- #

_DMP_THRESHOLD = 0.5


def _make_dmp(content_len: int) -> _DMP:
    dmp = _DMP()
    dmp.Match_Threshold = _DMP_THRESHOLD
    dmp.Match_Distance = max(content_len, 1000)
    dmp.Match_MaxBits = 0  # no pattern-size limit (default 32 breaks long patterns)
    return dmp


def apply_fuzzy_replace(content: str, old_string: str, new_string: str) -> tuple[str, int, int]:
    """Fuzzy-replace old_string with new_string inside content.

    Uses diff-match-patch for character-level location, then snaps to line
    boundaries so the replacement always covers complete source lines (same
    semantics as the previous Levenshtein window approach).

    Returns (new_content, 1-based line_start, 1-based line_end).
    """
    dmp = _make_dmp(len(content))

    match_char = dmp.match_main(content, old_string, 0)
    if match_char == -1:
        raise ValueError("old_string not found in file")

    # Build per-line character offsets
    lines = content.splitlines(keepends=True)
    offsets: list[int] = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    # Which line contains match_char?
    start_line_idx = max(0, bisect_right(offsets, match_char) - 1)

    # Replace the same number of lines as old_string spans
    n_old_lines = max(1, len(old_string.splitlines()))
    end_line_idx = min(start_line_idx + n_old_lines, len(lines))

    region_start = offsets[start_line_idx]
    region_end = offsets[end_line_idx]

    new_content = content[:region_start] + new_string + content[region_end:]
    return new_content, start_line_idx + 1, end_line_idx


# --------------------------------------------------------------------------- #
# Legacy find_fuzzy_candidates (thin wrapper — retained for compat)           #
# --------------------------------------------------------------------------- #


def find_fuzzy_candidates(
    content: str,
    old_string: str,
    *,
    distance_ratio: float = 0.05,
) -> list[FuzzyCandidate]:
    """Find candidate line windows — now backed by DMP. Returns 0 or 1 result."""
    dmp = _make_dmp(len(content))
    match_char = dmp.match_main(content, old_string, 0)
    if match_char == -1:
        return []

    lines = content.splitlines(keepends=True)
    offsets: list[int] = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    start_line_idx = max(0, bisect_right(offsets, match_char) - 1)
    n_old_lines = max(1, len(old_string.splitlines()))
    end_line_idx = min(start_line_idx + n_old_lines, len(lines))

    norm_old = normalize_for_fuzzy(old_string)
    window = "".join(lines[start_line_idx:end_line_idx])
    norm_window = normalize_for_fuzzy(window)
    ratio = SequenceMatcher(None, norm_old, norm_window, autojunk=False).ratio()

    return [
        FuzzyCandidate(
            start_line=start_line_idx + 1,
            end_line=end_line_idx,
            start_offset=offsets[start_line_idx],
            end_offset=offsets[end_line_idx],
            distance=0,
            ratio=ratio,
        )
    ]


__all__ = [
    "FuzzyAmbiguousMatchError",
    "FuzzyCandidate",
    "apply_fuzzy_replace",
    "bounded_levenshtein",
    "find_fuzzy_candidates",
    "normalize_for_fuzzy",
]
