"""Vector search helpers for Atelier.

Provides embedding generation and cosine similarity utilities used by retrieval.

Environment variables:
    ATELIER_VECTOR_SEARCH_ENABLED   1 | true | yes  to enable (default: false)
    ATELIER_EMBEDDING_DIM           embedding dimension (default: 1536)
    ATELIER_EMBEDDING_MODEL         model name hint (default: text-embedding-3-small)
    ATELIER_EMBEDDING_PROVIDER      local | openai (default: local)
    OPENAI_API_KEY                  required when provider=openai
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import urllib.request
from hashlib import sha256
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Optional numpy for embedding math — not required at import time
_numpy: Any = None
try:
    import numpy as _numpy_module

    _numpy = _numpy_module
except ImportError:
    # numpy is an optional dependency; vector/semantic features stay disabled when
    # it is absent. This is an expected configuration, not an error -- stay silent.
    _numpy = None


def is_vector_enabled() -> bool:
    """Return True when ATELIER_VECTOR_SEARCH_ENABLED is truthy."""
    return os.environ.get("ATELIER_VECTOR_SEARCH_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
    )


def get_embedding_dim() -> int:
    """Return the configured embedding dimension."""
    return int(os.environ.get("ATELIER_EMBEDDING_DIM", "1536"))


def get_embedding_model() -> str:
    """Return the configured embedding model name."""
    return os.environ.get("ATELIER_EMBEDDING_MODEL", "text-embedding-3-small")


def vector_cache_key(block_id: str, rendered_content: str) -> str:
    """Return a stable cache key for a rendered Playbook payload."""
    digest = sha256(rendered_content.encode("utf-8")).hexdigest()
    return f"{block_id}:{digest}"


def _vector_cache_path(root: str | Path) -> Path:
    return Path(root) / "vector_cache.sqlite"


def _ensure_vector_cache(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS playbook_embedding_cache (
            cache_key TEXT NOT NULL,
            embedder_name TEXT NOT NULL,
            vector_json TEXT NOT NULL,
            PRIMARY KEY (cache_key, embedder_name)
        )
        """)


def get_cached_embedding(root: str | Path, *, cache_key: str, embedder_name: str) -> list[float] | None:
    """Return a cached block embedding from the sidecar sqlite cache."""
    path = _vector_cache_path(root)
    if not path.exists():
        return None

    with sqlite3.connect(path) as conn:
        _ensure_vector_cache(conn)
        row = conn.execute(
            "SELECT vector_json FROM playbook_embedding_cache WHERE cache_key = ? AND embedder_name = ?",
            (cache_key, embedder_name),
        ).fetchone()

    if row is None:
        return None

    payload = json.loads(str(row[0]))
    if not isinstance(payload, list):
        return None
    return [float(item) for item in payload]


def put_cached_embedding(root: str | Path, *, cache_key: str, embedder_name: str, vector: list[float]) -> None:
    """Persist a block embedding into the sidecar sqlite cache."""
    path = _vector_cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(path) as conn:
        _ensure_vector_cache(conn)
        conn.execute(
            """
            INSERT INTO playbook_embedding_cache (cache_key, embedder_name, vector_json)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key, embedder_name) DO UPDATE SET vector_json = excluded.vector_json
            """,
            (cache_key, embedder_name, json.dumps(vector, ensure_ascii=False)),
        )
        conn.commit()


# One SQL placeholder per chunk row; stays well under SQLite's default
# SQLITE_MAX_VARIABLE_NUMBER (999 pre-3.32, 32766 after) across environments.
_CACHE_BATCH_CHUNK = 500


def get_cached_embeddings_batch(
    root: str | Path, *, cache_keys: list[str], embedder_name: str
) -> dict[str, list[float]]:
    """Look up many cache keys in a handful of queries on one connection.

    ``get_cached_embedding`` opens and closes a fresh sqlite connection per
    call, which is fine for one-off lookups but a severe bottleneck for bulk
    callers (e.g. index-time backfill of every symbol in a repo: 1.24M calls
    for linux, each paying connection-open overhead before any real work).
    Same cache, same schema -- just batched.
    """
    path = _vector_cache_path(root)
    if not path.exists() or not cache_keys:
        return {}

    results: dict[str, list[float]] = {}
    with sqlite3.connect(path) as conn:
        _ensure_vector_cache(conn)
        for start in range(0, len(cache_keys), _CACHE_BATCH_CHUNK):
            chunk = cache_keys[start : start + _CACHE_BATCH_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT cache_key, vector_json FROM playbook_embedding_cache "
                f"WHERE embedder_name = ? AND cache_key IN ({placeholders})",
                (embedder_name, *chunk),
            ).fetchall()
            for cache_key, vector_json in rows:
                payload = json.loads(str(vector_json))
                if isinstance(payload, list):
                    results[str(cache_key)] = [float(item) for item in payload]
    return results


def put_cached_embeddings_batch(
    root: str | Path, *, entries: list[tuple[str, list[float]]], embedder_name: str
) -> None:
    """Persist many embeddings in one connection/transaction instead of one each."""
    if not entries:
        return
    path = _vector_cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(path) as conn:
        _ensure_vector_cache(conn)
        conn.executemany(
            """
            INSERT INTO playbook_embedding_cache (cache_key, embedder_name, vector_json)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key, embedder_name) DO UPDATE SET vector_json = excluded.vector_json
            """,
            [(cache_key, embedder_name, json.dumps(vector, ensure_ascii=False)) for cache_key, vector in entries],
        )
        conn.commit()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return cosine similarity in [0, 1] between two vectors.

    Falls back to a pure-Python implementation when numpy is not installed.
    """
    if len(a) != len(b):
        raise ValueError(f"Vector dimension mismatch: {len(a)} vs {len(b)}")
    if _numpy is not None:
        va = _numpy.array(a, dtype="float64")
        vb = _numpy.array(b, dtype="float64")
        norm_a = _numpy.linalg.norm(va)
        norm_b = _numpy.linalg.norm(vb)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(_numpy.dot(va, vb) / (norm_a * norm_b))

    # Pure-Python fallback
    dot: float = sum((x * y for x, y in zip(a, b, strict=True)), 0.0)
    mag_a: float = sum((x * x for x in a), 0.0) ** 0.5
    mag_b: float = sum((x * x for x in b), 0.0) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _normalize(vec: list[float]) -> list[float]:
    mag = sum(x * x for x in vec) ** 0.5
    if mag == 0.0:
        return vec
    return [x / mag for x in vec]


def _local_embedding(text: str, *, dim: int) -> list[float]:
    """Generate a deterministic local embedding via feature hashing.

    This is a real text vectorization strategy (hashing trick), fully offline.
    """
    import hashlib

    vec = [0.0] * dim
    tokens = re.findall(r"[a-zA-Z0-9_]+", text.lower())
    if not tokens:
        return vec

    for token in tokens:
        # add unigram contribution
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if (digest[4] & 1) else -1.0
        vec[idx] += sign

    # add lightweight character 3-gram contribution for semantic smoothness
    compact = "_".join(tokens)
    for i in range(max(0, len(compact) - 2)):
        ngram = compact[i : i + 3]
        digest = hashlib.sha256(ngram.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if (digest[5] & 1) else -1.0
        vec[idx] += 0.25 * sign

    return _normalize(vec)


def _openai_embedding(text: str, *, dim: int) -> list[float]:
    """Generate embeddings using OpenAI's embeddings API."""
    import json

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required when ATELIER_EMBEDDING_PROVIDER=openai")

    body = {
        "model": get_embedding_model(),
        "input": text,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise RuntimeError("OpenAI embeddings response missing data")
    embedding = data[0].get("embedding")
    if not isinstance(embedding, list):
        raise RuntimeError("OpenAI embeddings response missing embedding vector")

    vec = [float(x) for x in embedding]
    if len(vec) != dim:
        vec = vec[:dim] if len(vec) > dim else vec + [0.0] * (dim - len(vec))
    return _normalize(vec)


def generate_embedding(text: str, *, dim: int | None = None) -> list[float]:
    """Generate an embedding using configured provider.

    Provider selection:
      - local  (default): deterministic feature-hashing embedding
      - openai: OpenAI embeddings endpoint
    """
    resolved_dim = dim or get_embedding_dim()
    provider = os.environ.get("ATELIER_EMBEDDING_PROVIDER", "local").strip().lower()
    if provider == "openai":
        return _openai_embedding(text, dim=resolved_dim)
    return _local_embedding(text, dim=resolved_dim)


__all__ = [
    "cosine_similarity",
    "generate_embedding",
    "get_cached_embedding",
    "get_embedding_dim",
    "get_embedding_model",
    "is_vector_enabled",
    "put_cached_embedding",
    "vector_cache_key",
]
