"""Tests for prompt_compilation.tokens (P0)."""

from __future__ import annotations

import pytest

from atelier.core.capabilities.prompt_compilation.tokens import estimate_tokens


class TestTiktokenUsedWhenAvailable:
    def test_nonempty_returns_positive(self) -> None:
        n = estimate_tokens("hello world")
        assert n > 0

    def test_empty_returns_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_longer_text_more_tokens(self) -> None:
        short = estimate_tokens("hi")
        long_ = estimate_tokens("hi " * 100)
        assert long_ > short

    def test_model_param_accepted(self) -> None:
        # model param is reserved; must not raise
        n = estimate_tokens("hello", model="gpt-4o")
        assert n > 0


class TestCharFallbackWithin15Percent:
    """When tiktoken is absent the char/4 fallback must be within 15% of tiktoken."""

    SAMPLE = "The quick brown fox jumps over the lazy dog. " * 20

    def _char_fallback(self, text: str) -> int:
        return max(1, len(text) // 4)

    def test_fallback_within_15_percent_of_tiktoken(self) -> None:
        try:
            import tiktoken  # noqa: F401
        except ImportError:
            pytest.skip("tiktoken not installed")

        tiktoken_n = estimate_tokens(self.SAMPLE)
        fallback_n = self._char_fallback(self.SAMPLE)
        ratio = abs(tiktoken_n - fallback_n) / tiktoken_n
        assert ratio < 0.15, f"char/4 fallback ({fallback_n}) deviates {ratio:.1%} from " f"tiktoken ({tiktoken_n})"
