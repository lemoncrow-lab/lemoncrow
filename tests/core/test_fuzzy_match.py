"""Tests for fuzzy matching utilities used by batch_edit (WP-24)."""

from __future__ import annotations

import pytest

from atelier.core.capabilities.tool_supervision.fuzzy_match import (
    FuzzyAmbiguousMatchError,
    apply_fuzzy_replace,
    normalize_for_fuzzy,
)


def test_normalize_for_fuzzy_tolerates_whitespace_variants() -> None:
    left = "\tdef fn( )  :   \n    return 1   "
    right = "    def fn( ):\n\treturn   1"
    assert normalize_for_fuzzy(left) == normalize_for_fuzzy(right)


def test_apply_fuzzy_replace_handles_indentation_drift() -> None:
    content = "def outer():\n\tif True:\n\t\treturn 1\n"
    old = "def outer():\n    if True:\n        return 1\n"
    new = "def outer():\n\tif True:\n\t\treturn 2\n"

    updated, line_start, line_end = apply_fuzzy_replace(content, old, new)

    assert "return 2" in updated
    assert line_start == 1
    assert line_end == 3


def test_apply_fuzzy_replace_refuses_ambiguous_duplicate_blocks() -> None:
    """Near-miss old_string over duplicated blocks must not silently mis-anchor.

    Regression: DMP returned the *first* acceptable window, so a near-miss
    old_string targeting the *second* of two similar blocks silently replaced
    the first. The scan now ranks every window and refuses on a tie.
    """
    shared = (
        "long shared paragraph line one\n"
        "long shared paragraph line two\n"
        "long shared paragraph line three\n"
        "long shared paragraph line four\n"
    )
    content = "HEADER_B1 marker\n" + shared + "HEADER_B2 marker\n" + shared + "TAIL\n"
    old = "HEADER_B2 markerX\n" + shared  # near-miss targeting block 2

    with pytest.raises(FuzzyAmbiguousMatchError):
        apply_fuzzy_replace(content, old, "REPLACED\n")
    # block 1 is never silently clobbered
    assert "HEADER_B1 marker" in content


def test_apply_fuzzy_replace_refuses_identical_block_tie() -> None:
    """Two identical blocks + a near-miss that matches both equally -> refuse."""
    block = "shared line a\nshared line b\nshared line c\nshared line d\n"
    content = "top\n" + block + "mid\n" + block + "end\n"
    old = "shared line a\nshared line b\nshared line c\nshared line dX\n"

    with pytest.raises(FuzzyAmbiguousMatchError):
        apply_fuzzy_replace(content, old, "REPLACED\n")


def test_apply_fuzzy_replace_handles_trailing_whitespace_drift() -> None:
    content = "SELECT id, name   \nFROM users\n"
    old = "SELECT id, name\nFROM users\n"
    new = "SELECT id\nFROM users\n"

    updated, line_start, line_end = apply_fuzzy_replace(content, old, new)

    assert updated == new
    assert line_start == 1
    assert line_end == 2


def test_apply_fuzzy_replace_handles_blank_line_drift() -> None:
    content = "def outer():\n\n\tif True:\n\t\treturn 1\n"
    old = "def outer():\n    if True:\n        return 1\n"
    new = "def outer():\n    if True:\n        return 2\n"

    updated, line_start, line_end = apply_fuzzy_replace(content, old, new)

    assert "return 2" in updated
    assert line_start == 1
    assert line_end == 4


def test_apply_fuzzy_replace_raises_ambiguous_on_duplicate_blocks() -> None:
    """Duplicate equally-good blocks: surface ambiguity instead of picking one arbitrarily."""
    content = "def hello():\n    return 1\n\ndef hello():\n    return 1\n"
    old = "def hello():\n\treturn 1\n"
    new = "def hello():\n    return 2\n"

    with pytest.raises(FuzzyAmbiguousMatchError) as excinfo:
        apply_fuzzy_replace(content, old, new)

    ranges = {(c.start_line, c.end_line) for c in excinfo.value.candidates}
    assert (1, 2) in ranges and (4, 5) in ranges
