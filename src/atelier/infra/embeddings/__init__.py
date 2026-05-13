"""Embedding backends."""

from __future__ import annotations

from atelier.infra.embeddings.base import Embedder, EmbedResult
from atelier.infra.embeddings.factory import (
    LettaEmbedder,
    NullEmbedder,
    OpenAIEmbedder,
    make_embedder,
)

__all__ = [
    "EmbedResult",
    "Embedder",
    "LettaEmbedder",
    "NullEmbedder",
    "OpenAIEmbedder",
    "make_embedder",
]
