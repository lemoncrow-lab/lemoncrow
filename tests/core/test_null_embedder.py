"""Tests for NullEmbedder."""

from __future__ import annotations

from atelier.infra.embeddings.base import Embedder
from atelier.infra.embeddings.null_embedder import NullEmbedder


def test_null_embedder_returns_empty_vectors() -> None:
    e = NullEmbedder()
    result = e.embed(["hello", "world"])
    assert result == [[], []]


def test_null_embedder_returns_empty_list_for_empty_input() -> None:
    e = NullEmbedder()
    assert e.embed([]) == []


def test_null_embedder_dim_is_zero() -> None:
    e = NullEmbedder()
    assert e.dim == 0


def test_null_embedder_name() -> None:
    e = NullEmbedder()
    assert e.name == "null"


def test_null_embedder_satisfies_protocol() -> None:
    e = NullEmbedder()
    assert isinstance(e, Embedder)
