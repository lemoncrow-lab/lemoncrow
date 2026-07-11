"""NomicEmbedder — nomic-ai/nomic-embed-code via SentenceTransformers.

Matches the BgeEmbedder interface exactly so it can be swapped in via
``LEMONCROW_CODE_EMBEDDER=nomic``.  Uses the standard Nomic task prefixes:
  * queries   → "search_query: <text>"
  * documents → "search_document: <text>"

768-dimensional; the same model bundled (int8) inside codebase-memory-mcp,
but loaded here at full fp16/fp32 precision for a fair quality comparison.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "nomic-ai/nomic-embed-code"
_QUERY_PREFIX = "search_query: "
_DOC_PREFIX = "search_document: "

# nomic-embed-code is a Matryoshka model: 3584d at full precision; the
# codebase-memory-mcp binary truncates to 768d (int8).  Set
# LEMONCROW_NOMIC_DIM=768 to replicate the CMM truncation, or leave unset for
# full-precision 3584d vectors (better quality, higher memory).
_DEFAULT_DIM_FULL = 3584
_DEFAULT_DIM_TRUNCATED = 768


class NomicEmbedder:
    """nomic-embed-code via SentenceTransformers.

    FP16 on CUDA, FP32 on CPU.  Normalises to unit length.  Lazy-loads on
    first ``embed()`` call so import-time stays fast.

    Set LEMONCROW_NOMIC_DIM=768 to match codebase-memory-mcp's truncated vectors.
    Leave unset for full 3584d Matryoshka precision.
    """

    dim: int
    name: str

    def __init__(
        self,
        model_name: str = _DEFAULT_MODEL,
        *,
        query_prefix: str | None = None,
        doc_prefix: str | None = None,
        use_cache: bool = True,
    ) -> None:
        self._model_name = model_name
        self._model: Any = None
        self._use_cache = use_cache  # reserved for future use
        _env_dim = os.environ.get("LEMONCROW_NOMIC_DIM", "")
        self.dim = int(_env_dim) if _env_dim.isdigit() else _DEFAULT_DIM_FULL
        # Per-instance prefixes: constructor args > env vars > Nomic defaults.
        self._query_prefix = (
            query_prefix if query_prefix is not None else os.environ.get("LEMONCROW_HF_QUERY_PREFIX", _QUERY_PREFIX)
        )
        self._doc_prefix = (
            doc_prefix if doc_prefix is not None else os.environ.get("LEMONCROW_HF_DOC_PREFIX", _DOC_PREFIX)
        )
        self.name = f"nomic:{model_name}" + (f"@{self.dim}d" if self.dim != _DEFAULT_DIM_FULL else "")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        raw = vecs.tolist() if hasattr(vecs, "tolist") else list(vecs)
        # Matryoshka truncation: slice to self.dim and re-normalise.
        if self.dim < len(raw[0]):
            import math

            out = []
            for v in raw:
                t = v[: self.dim]
                norm = math.sqrt(sum(x * x for x in t)) or 1.0
                out.append([x / norm for x in t])
            return out
        return raw

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Embeds *queries* with the configured query prefix."""
        if not texts:
            return []
        return self.embed([f"{self._query_prefix}{t}" for t in texts])

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embeds *documents* with the configured doc prefix."""
        if not texts:
            return []
        return self.embed([f"{self._doc_prefix}{t}" for t in texts])

    @classmethod
    def is_available(cls) -> bool:
        """True when sentence_transformers + torch are importable.  Does NOT load the model."""
        try:
            import sentence_transformers  # noqa: F401
            import torch  # noqa: F401

            return True
        except ImportError:
            return False

    @property
    def is_ready(self) -> bool:
        """True once the model has been loaded into memory."""
        return self._model is not None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        import torch
        from sentence_transformers import SentenceTransformer

        device = "cuda" if torch.cuda.is_available() else "cpu"
        # Compatibility shim: transformers 4.47+ removed DynamicCache.get_usable_length
        # (renamed to get_seq_length).  Some model remote_code still calls the old name.
        try:
            from transformers.cache_utils import DynamicCache as _DC

            if not hasattr(_DC, "get_usable_length"):
                _DC.get_usable_length = lambda self, new_seq_length=0, layer_idx=0: self.get_seq_length(layer_idx)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

        kw = {"dtype": torch.float16} if device == "cuda" else {}
        logger.info(
            "Loading Nomic model %s on %s (FP16=%s)",
            self._model_name,
            device,
            device == "cuda",
        )
        model = SentenceTransformer(
            self._model_name,
            trust_remote_code=True,
            device=device,
            model_kwargs=kw,
        )
        model.eval()
        model.max_seq_length = int(os.environ.get("LEMONCROW_NOMIC_MAX_SEQ", "8192"))
        if device == "cuda":
            model.half()
        self._model = model
        return model


__all__ = ["NomicEmbedder"]
