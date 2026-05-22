"""Semantic ranking helpers for mode-aware code search."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from atelier.core.capabilities.code_context.models import SymbolRecord
from atelier.core.foundation.paths import default_store_root
from atelier.infra.embeddings.local import LocalEmbedder
from atelier.infra.embeddings.null_embedder import NullEmbedder
from atelier.infra.storage.vector import (
    cosine_similarity,
    get_cached_embedding,
    put_cached_embedding,
    vector_cache_key,
)

SearchMode = Literal["auto", "lexical", "semantic", "hybrid"]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_:.]*$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_STOP_WORDS = frozenset({"a", "an", "for", "how", "in", "of", "the", "to", "with"})
_DEFAULT_RRF_K = 60
_DEFAULT_CANDIDATE_LIMIT = 200


@dataclass
class _FusionEntry:
    symbol: SymbolRecord
    score: float
    lexical_rank: int | None = None
    semantic_rank: int | None = None


def is_identifier_query(query: str) -> bool:
    """Return True when the query looks like a symbol identifier."""
    stripped = query.strip()
    return bool(stripped) and bool(_IDENTIFIER_RE.fullmatch(stripped))


def looks_natural_language_query(query: str) -> bool:
    """Return True when the query should auto-promote to hybrid search."""
    tokens = [token.lower() for token in _TOKEN_RE.findall(query)]
    return " " in query.strip() or any(token in _STOP_WORDS for token in tokens)


def resolve_search_mode(query: str, requested_mode: SearchMode) -> Literal["lexical", "semantic", "hybrid"]:
    """Resolve the effective search mode for a query."""
    if requested_mode != "auto":
        return requested_mode
    if is_identifier_query(query):
        return "lexical"
    if looks_natural_language_query(query):
        return "hybrid"
    return "lexical"


def semantic_candidate_limit(limit: int) -> int:
    """Cap semantic candidate generation to protect search latency."""
    return max(limit, min(_DEFAULT_CANDIDATE_LIMIT, max(limit * 5, 25)))


def render_embedding_text(symbol: SymbolRecord, *, source_text: str | None = None) -> str:
    """Render the text used to embed a symbol."""
    source = (source_text or "").strip().replace("\x00", " ")
    if len(source) > 200:
        source = source[:200]
    parts = [symbol.symbol_name, symbol.signature]
    if symbol.doc_summary:
        parts.append(symbol.doc_summary)
    elif source:
        parts.append(source)
    return "\n".join(part for part in parts if part).strip()


class SemanticSearchRanker:
    """Deterministic local semantic ranking with cached vectors."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        store_root: str | Path | None = None,
        embedder: LocalEmbedder | NullEmbedder | None = None,
        rrf_k: int = _DEFAULT_RRF_K,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.store_root = Path(store_root) if store_root is not None else default_store_root()
        self.embedder = embedder or LocalEmbedder()
        self.rrf_k = rrf_k

    def semantic_search(
        self,
        query: str,
        *,
        candidates: Sequence[SymbolRecord],
        limit: int,
        source_loader: Callable[[SymbolRecord], str],
    ) -> list[SymbolRecord]:
        """Rank candidate symbols by cosine similarity to the query embedding."""
        query_vector = self._embed_query(query)
        if not query_vector:
            return []

        scored: list[tuple[float, SymbolRecord]] = []
        for symbol in candidates:
            source_text = source_loader(symbol)
            embedding_text = render_embedding_text(symbol, source_text=source_text)
            if not embedding_text:
                continue
            symbol_vector = self._embed_symbol(symbol, embedding_text)
            if not symbol_vector:
                continue
            score = cosine_similarity(query_vector, symbol_vector)
            if score <= 0:
                continue
            scored.append((score, symbol.model_copy(update={"score": score})))

        scored.sort(key=lambda item: (-item[0], item[1].file_path, item[1].start_line))
        return [symbol for _, symbol in scored[:limit]]

    def reciprocal_rank_fuse(
        self,
        lexical_hits: Sequence[SymbolRecord],
        semantic_hits: Sequence[SymbolRecord],
        *,
        limit: int,
    ) -> list[SymbolRecord]:
        """Fuse lexical and semantic rankings with reciprocal rank fusion."""
        fused: dict[str, _FusionEntry] = {}
        for rank, symbol in enumerate(lexical_hits, start=1):
            entry = fused.setdefault(
                symbol.symbol_id,
                _FusionEntry(symbol=symbol, score=0.0, lexical_rank=rank),
            )
            entry.score += 1.0 / (self.rrf_k + rank)
        for rank, symbol in enumerate(semantic_hits, start=1):
            entry = fused.setdefault(
                symbol.symbol_id,
                _FusionEntry(symbol=symbol, score=0.0, semantic_rank=rank),
            )
            entry.score += 1.0 / (self.rrf_k + rank)
            if entry.lexical_rank is None:
                entry.symbol = symbol
            entry.semantic_rank = rank

        ordered = sorted(
            fused.values(),
            key=lambda entry: (
                -entry.score,
                entry.semantic_rank or 10_000,
                entry.lexical_rank or 10_000,
                entry.symbol.file_path,
                entry.symbol.start_line,
            ),
        )
        return [entry.symbol.model_copy(update={"score": entry.score}) for entry in ordered[:limit]]

    def _embed_query(self, query: str) -> list[float]:
        cache_key = vector_cache_key("code-search-query", f"{self.embedder.name}:{query.strip().lower()}")
        return self._embed_text(query, cache_key=cache_key)

    def _embed_symbol(self, symbol: SymbolRecord, embedding_text: str) -> list[float]:
        cache_key = vector_cache_key(symbol.symbol_id, f"{self.embedder.name}:{symbol.content_hash}:{embedding_text}")
        return self._embed_text(embedding_text, cache_key=cache_key)

    def _embed_text(self, text: str, *, cache_key: str) -> list[float]:
        if self.embedder.dim <= 0:
            return []
        cached = get_cached_embedding(self.store_root, cache_key=cache_key, embedder_name=self.embedder.name)
        if cached is not None:
            return cached
        vector = [float(value) for value in self.embedder.embed([text])[0]]
        put_cached_embedding(self.store_root, cache_key=cache_key, embedder_name=self.embedder.name, vector=vector)
        return vector


__all__ = [
    "SearchMode",
    "SemanticSearchRanker",
    "is_identifier_query",
    "looks_natural_language_query",
    "render_embedding_text",
    "resolve_search_mode",
    "semantic_candidate_limit",
]
