"""BgeEmbedder — local BGE-Code-v1 via SentenceTransformers (HF, not GGUF/Ollama).

Loads the model lazily on first ``embed()`` call so import-time is fast and
memory stays low when a different embedder is configured.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "BAAI/bge-code-v1"
_DEFAULT_DIM = 1536

_QUERY_PREFIX = "<instruct>Given a natural language query, retrieve relevant code.\n<query>"


class BgeEmbedder:
    """HF BGE-Code-v1 via SentenceTransformers.

    Uses FP16 on CUDA, FP32 on CPU.  Normalises embeddings to unit length.
    """

    dim: int
    name: str

    def __init__(self, model_name: str = _DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self._model: Any = None
        self.dim = _DEFAULT_DIM
        self.name = f"bge:{model_name}"

    # ── public API ──────────────────────────────────────────────────────

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return vecs.tolist() if hasattr(vecs, "tolist") else list(vecs)

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Embeds *queries* with the BGE instruction prefix."""
        if not texts:
            return []
        prefixed = [f"{_QUERY_PREFIX}{t}" for t in texts]
        return self.embed(prefixed)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embeds *documents* without any prefix."""
        return self.embed(texts)

    # ── internals ───────────────────────────────────────────────────────

    @classmethod
    def is_available(cls) -> bool:
        """Return True when sentence_transformers and torch are importable.

        Does NOT load the model; safe to call at import time for feature
        detection.  Used by ``make_code_embedder`` to auto-select BGE when the
        extras are installed and no explicit pin is set.
        """
        try:
            import sentence_transformers  # noqa: F401
            import torch  # noqa: F401

            return True
        except ImportError:
            return False

    @property
    def is_ready(self) -> bool:
        """True once the model has been loaded into memory.

        Callers that want to skip semantic search while the model is still
        warming up should check this before calling ``embed()``.
        """
        return self._model is not None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        import torch
        from sentence_transformers import SentenceTransformer

        device = "cuda" if torch.cuda.is_available() else "cpu"
        kw = {"dtype": torch.float16} if device == "cuda" else {}
        logger.info("Loading BGE model %s on %s (FP16=%s)", self._model_name, device, device == "cuda")
        model = SentenceTransformer(
            self._model_name,
            trust_remote_code=True,
            device=device,
            model_kwargs=kw,
        )
        model.eval()
        # Cap sequence length: a few symbols have enormous source whose full token
        # sequence would blow GPU memory (attention is O(seq^2)). 2048 tokens covers
        # virtually every real symbol; longer ones truncate. Override via env.
        model.max_seq_length = int(os.environ.get("LEMONCROW_BGE_MAX_SEQ", "2048"))
        if device == "cuda":
            model.half()
        self._model = model
        return model


__all__ = ["BgeEmbedder"]
