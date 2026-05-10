"""LocalEmbedder — sentence-transformers backend (optional dep)."""

from __future__ import annotations

import contextlib
import io
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

_MODEL_CACHE: dict[tuple[str, str], Any] = {}

_DEFAULT_MODEL = "all-MiniLM-L6-v2"
_DEFAULT_DIM = 384


class LocalEmbedder:
    """Sentence-transformers embedder; lazy-loads model on first use."""

    dim: int
    name: str

    def __init__(self, model_name: str = _DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self._device = os.environ.get("ATELIER_LOCAL_EMBEDDER_DEVICE", "cpu")
        self.dim = _DEFAULT_DIM
        self.name = f"local:{model_name}"

    def _get_model(self) -> Any:
        cache_key = (self._model_name, self._device)
        if cache_key not in _MODEL_CACHE:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise ImportError(
                    "sentence-transformers is not installed; run: pip install 'atelier[embeddings]'"
                ) from exc
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                model = SentenceTransformer(self._model_name, device=self._device)
            _MODEL_CACHE[cache_key] = model
        return _MODEL_CACHE[cache_key]

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            model = self._get_model()
        except ImportError:
            from atelier.infra.storage.vector import generate_embedding

            return [generate_embedding(text, dim=self.dim) for text in texts]
        vectors = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return [v.tolist() for v in vectors]


__all__ = ["LocalEmbedder"]
