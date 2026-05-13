"""LocalEmbedder — deterministic offline embedding backend."""

from __future__ import annotations

_DEFAULT_MODEL = "hashing"
_DEFAULT_DIM = 384


class LocalEmbedder:
    """Deterministic local embedder based on feature hashing."""

    dim: int
    name: str

    def __init__(self, model_name: str = _DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self.dim = _DEFAULT_DIM
        self.name = f"local:{model_name}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        from atelier.infra.storage.vector import generate_embedding

        return [generate_embedding(text, dim=self.dim) for text in texts]


__all__ = ["LocalEmbedder"]
