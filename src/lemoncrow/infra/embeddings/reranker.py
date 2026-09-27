"""Cross-encoder reranker (BGE-reranker via SentenceTransformers).

A cross-encoder re-scores each ``(query, candidate_code)`` pair by reading both
together, so it can promote a relevant file the bi-encoder cosine ranked low --
recovering rank the retrieval stage lost. Reranking reorders an ALREADY-retrieved
candidate pool; it cannot add a file the retriever missed (its ceiling is the
pool's recall).

Lazy-loads on first ``rerank()`` so import stays cheap and no model is pulled
when reranking is disabled. Mirrors ``BgeEmbedder``: SentenceTransformers,
FP16 on CUDA / FP32 on CPU.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"
_T = TypeVar("_T")


class BgeReranker:
    """Cross-encoder reranker over (query, passage) pairs.

    ``rerank(query, passages)`` returns a relevance score per passage (higher =
    more relevant). ``rerank_items`` reorders arbitrary objects by a text key.
    Scores are model logits, not probabilities -- only their order is meaningful,
    which is all a reranker needs.
    """

    name: str

    def __init__(self, model_name: str = _DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self._model = None
        self.name = f"reranker:{model_name}"

    @classmethod
    def is_available(cls) -> bool:
        """True when sentence_transformers + torch are importable (no model load)."""
        try:
            import sentence_transformers  # noqa: F401
            import torch  # noqa: F401

            return True
        except ImportError:
            return False

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        import torch
        from sentence_transformers import CrossEncoder

        device = "cuda" if torch.cuda.is_available() else "cpu"
        max_len = int(os.environ.get("LEMONCROW_RERANK_MAX_SEQ", "1024"))
        logger.info("Loading reranker %s on %s", self._model_name, device)
        model = CrossEncoder(
            self._model_name,
            trust_remote_code=True,
            device=device,
            max_length=max_len,
        )
        self._model = model
        return model

    def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        """Return a relevance score for each passage (same order as input)."""
        if not passages:
            return []
        model = self._load()
        batch = int(os.environ.get("LEMONCROW_RERANK_BATCH", "32"))
        scores = model.predict(
            [(query, p) for p in passages],
            batch_size=batch,
            show_progress_bar=False,
        )
        return [float(s) for s in (scores.tolist() if hasattr(scores, "tolist") else scores)]

    def rerank_items(
        self,
        query: str,
        items: Sequence[_T],
        *,
        text_of: Callable[[_T], str],
        top_k: int | None = None,
    ) -> list[tuple[_T, float]]:
        """Reorder ``items`` by cross-encoder relevance to ``query``.

        ``text_of`` renders each item to the passage text scored against the
        query. Returns ``(item, score)`` sorted by descending score; ``top_k``
        caps how many items are scored (rerank only the retrieval head -- the
        tail is rarely the answer and scoring it wastes model calls).
        """
        pool = list(items) if top_k is None else list(items)[:top_k]
        if not pool:
            return []
        scores = self.rerank(query, [text_of(it) for it in pool])
        order = sorted(range(len(pool)), key=lambda i: -scores[i])
        return [(pool[i], scores[i]) for i in order]


__all__ = ["BgeReranker"]
