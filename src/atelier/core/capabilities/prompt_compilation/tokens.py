"""Token estimation for prompt blocks.

Uses tiktoken (cl100k_base) when available, with a char/4 fallback.
Pattern is consistent with core/capabilities/repo_map/budget.py.
"""

from __future__ import annotations

from typing import Protocol


class _Encoder(Protocol):
    def encode(self, text: str) -> list[int]: ...


class _UnavailableEncoding:
    pass


_UNAVAILABLE = _UnavailableEncoding()
_ENCODING: _Encoder | _UnavailableEncoding | None = None


def _get_encoding() -> _Encoder | None:
    """Return the tiktoken encoding, loading it once on first call."""
    global _ENCODING
    if _ENCODING is None:
        try:
            import tiktoken

            _ENCODING = tiktoken.get_encoding("cl100k_base")
        except (AttributeError, ImportError, KeyError, ValueError):  # pragma: no cover
            _ENCODING = _UNAVAILABLE
    if isinstance(_ENCODING, _UnavailableEncoding):
        return None
    return _ENCODING


def estimate_tokens(text: str, model: str | None = None) -> int:
    """Estimate the token count of *text*.

    Args:
        text: The text to estimate.
        model: Ignored in this implementation (reserved for future per-provider
               tokenizers). The cl100k_base encoding is used regardless.

    Returns:
        An integer token estimate ≥ 0.
    """
    del model
    if not text:
        return 0
    enc = _get_encoding()
    if enc is not None:
        return len(enc.encode(text))
    # Char/4 fallback — within ~15% for English prose and code.
    return max(1, len(text) // 4)
