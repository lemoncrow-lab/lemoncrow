"""Embedding protocol for memory and recall capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class EmbedResult:
    text: str
    vector: list[float]


@runtime_checkable
class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


__all__ = ["EmbedResult", "Embedder"]
