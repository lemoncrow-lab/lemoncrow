"""Token budget fitting for repo maps."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import tiktoken

_ENCODING: tiktoken.Encoding | None = None


def count_tokens(text: str) -> int:
    # Lazy: loading the cl100k BPE table costs ~80ms, so keep it off module import.
    global _ENCODING
    if _ENCODING is None:
        import tiktoken

        _ENCODING = tiktoken.get_encoding("cl100k_base")
    return len(_ENCODING.encode(text))


def estimate_tokens(text: str) -> int:
    """Fast char-based token approximation for budget *gating*, where an exact
    BPE count is unnecessary. cl100k averages ~3.6 chars/token on source code,
    so dividing by 3.6 (rounded up) trends slightly conservative (it never badly
    under-counts) while costing ~50x less than ``count_tokens`` -- which matters
    in hot loops that re-measure a growing context once per binary-search step."""
    return -(-len(text) * 10 // 36)  # ceil(len / 3.6)


def fit_to_budget(
    ranked_files: list[str], render: Callable[[list[str]], str], budget_tokens: int
) -> tuple[list[str], str]:
    lo = 0
    hi = len(ranked_files)
    best_files: list[str] = []
    best_text = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        current_files = ranked_files[:mid]
        text = str(render(current_files))
        if count_tokens(text) <= budget_tokens:
            best_files = current_files
            best_text = text
            lo = mid + 1
        else:
            hi = mid - 1
    if not best_files and ranked_files:
        best_files = ranked_files[:1]
        best_text = str(render(best_files))
    return best_files, best_text


__all__ = ["count_tokens", "estimate_tokens", "fit_to_budget"]
