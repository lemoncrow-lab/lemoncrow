"""Embedding backends."""

from __future__ import annotations

from lemoncrow.infra.embeddings.base import Embedder, EmbedResult
from lemoncrow.infra.embeddings.bge import BgeEmbedder
from lemoncrow.infra.embeddings.factory import (
    DEFAULT_CODE_EMBED_MODEL,
    NullEmbedder,
    OllamaEmbedder,
    OpenAIEmbedder,
    get_code_embedder,
    get_embedder,
    make_code_embedder,
    make_embedder,
)
from lemoncrow.infra.embeddings.letta_embedder import LettaEmbedder

__all__ = [
    "DEFAULT_CODE_EMBED_MODEL",
    "BgeEmbedder",
    "EmbedResult",
    "Embedder",
    "LettaEmbedder",
    "NullEmbedder",
    "OllamaEmbedder",
    "OpenAIEmbedder",
    "get_code_embedder",
    "get_embedder",
    "make_code_embedder",
    "make_embedder",
]
