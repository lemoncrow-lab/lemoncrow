"""Unit tests for cap_source_by_tokens: token-budget, line-boundary source capping."""

from __future__ import annotations

from lemoncrow.core.capabilities.code_context.output_policy import (
    TRUNCATION_MARKER,
    cap_source_by_tokens,
)
from lemoncrow.core.capabilities.repo_map.budget import estimate_tokens


def _numbered(start: int, n: int, width: int = 40) -> str:
    return "\n".join(f"{start + i}\t{'a' * width}" for i in range(n))


def test_fits_whole_returns_unchanged() -> None:
    text = _numbered(10, 3)
    out = cap_source_by_tokens(text, 10_000, estimate_tokens, start_line=10, end_line=12)
    assert out == text


def test_cuts_on_line_boundary_with_precise_marker() -> None:
    text = _numbered(100, 20)  # ~20 lines, well over budget
    out = cap_source_by_tokens(text, 60, estimate_tokens, start_line=100, end_line=119)
    body, _, marker = out.rpartition("\n")
    # marker names where the cut fell and the symbol's end line -- no start line.
    assert marker.startswith("... [truncated after L")
    assert marker.endswith("— L119]")
    kept = body.split("\n")
    original = text.split("\n")
    # every kept line is a whole original line (no mid-line cut)
    assert kept == original[: len(kept)]
    # the reported "after L" equals the last whole line actually kept
    assert marker == f"... [truncated after L{100 + len(kept) - 1} — L119]"


def test_long_first_line_is_hard_cut_not_dropped() -> None:
    # A single line whose own token cost exceeds the whole budget must not vanish
    # or blow the budget -- its head survives with an inline ellipsis.
    line = "5\t" + "x" * 4000
    out = cap_source_by_tokens(line, 12, estimate_tokens, start_line=5, end_line=5)
    assert out.startswith("5\txxxx")
    assert " ..." in out
    assert out.endswith("... [truncated after L5 — L5]")
    # budget actually held: the emitted head is far shorter than the input line
    assert len(out) < len(line)


def test_nonpositive_budget_returns_bare_marker() -> None:
    assert cap_source_by_tokens("5\tcode", 0, estimate_tokens, start_line=5, end_line=5) == TRUNCATION_MARKER
