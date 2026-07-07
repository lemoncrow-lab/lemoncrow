"""Null embedding backend for FTS-only operation."""

from __future__ import annotations


class NullEmbedder:
    dim = 0
    name = "null"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]


__all__ = ["NullEmbedder"]
