"""Embedding helper for Context Lineage commit summaries.

Uses LocalEmbedder (384-dim) — the SAME embedder as SemanticSearchRanker —
so commit and symbol embeddings are directly comparable in ranking.

CRITICAL: Do NOT use make_embedder() or generate_embedding() from
infra/storage/vector.py — those may use ATELIER_EMBEDDING_DIM (default
1536) which is a different dimension. Always instantiate LocalEmbedder
directly.
"""

from __future__ import annotations

import struct

from atelier.infra.code_intel.git_history.models import CommitSummary
from atelier.infra.embeddings.local import LocalEmbedder

_DIM = 384
_EMBEDDER: LocalEmbedder | None = None


def _get_embedder() -> LocalEmbedder:
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = LocalEmbedder()
    return _EMBEDDER


def embed_summary(summary: CommitSummary) -> bytes:
    """Embed the summary text + top-10 files into a 384-dim float32 BLOB.

    Text format: "{summary}\\n{space-joined files[:10]}"
    Storage: struct.pack(f'{dim}f', *vector) — little-endian float32.
    """
    text = f"{summary.summary}\n{' '.join(summary.files_touched[:10])}"
    embedder = _get_embedder()
    vectors = embedder.embed([text])
    vec = vectors[0]
    return struct.pack(f"{len(vec)}f", *vec)


def decode_embedding(blob: bytes) -> list[float]:
    """Deserialise a BLOB back to list[float]."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def embedding_dim() -> int:
    """Return the expected embedding dimension (384)."""
    return _DIM


__all__ = ["decode_embedding", "embed_summary", "embedding_dim"]
