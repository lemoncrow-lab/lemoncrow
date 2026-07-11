"""Embedding backend factory."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Protocol, runtime_checkable

from lemoncrow.core.environment import resolve_memory_backend
from lemoncrow.core.foundation.paths import default_store_root

from .base import Embedder
from .bge import BgeEmbedder
from .letta_embedder import LettaEmbedder
from .nomic import NomicEmbedder
from .null_embedder import NullEmbedder
from .ollama_embedder import DEFAULT_CODE_EMBED_MODEL, OllamaEmbedder
from .openai_embedder import OpenAIEmbedder

logger = logging.getLogger(__name__)

_PIN_CHOICES = frozenset({"openai", "letta", "null"})
_CODE_PIN_CHOICES = frozenset({"openai", "letta", "null", "ollama", "bge", "nomic", "hf"})

# Auto-select fallback for CPU-only / low-VRAM machines when no embedder pin
# is set. Real embedding dim is 1024 -- see BENCHMARKS.md's embedder sweep.
_SFR_FALLBACK_MODEL = "Salesforce/SFR-Embedding-Code-400M_R"
_SFR_FALLBACK_DIM = 1024
_BGE_MIN_FREE_VRAM_MB = 4096


def _gpu_has_sufficient_vram_for_bge(min_free_mb: int = _BGE_MIN_FREE_VRAM_MB) -> bool:
    """True when a CUDA GPU with at least *min_free_mb* free VRAM is present.

    BGE-Code-v1 is a ~1.5B-param model; below this threshold (or with no GPU
    at all) it is impractically slow to load/run, so the auto-select default
    falls back to the much smaller SFR-Embedding-Code-400M_R instead.
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return False
        free_bytes, _ = torch.cuda.mem_get_info()
        return (free_bytes // (1024 * 1024)) >= min_free_mb
    except Exception:  # noqa: BLE001 -- torch not installed or query failed
        return False


@runtime_checkable
class TaskAwareEmbedder(Protocol):
    def embed_queries(self, texts: list[str]) -> list[list[float]]: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


def make_embedder(pin: str | None = None) -> Embedder:
    """Return the memory-path embedder without changing existing selection rules."""
    raw_choice = pin if pin is not None else (os.environ.get("LEMONCROW_EMBEDDER") or "")
    chosen = raw_choice.strip().lower()

    if chosen:
        if chosen not in _PIN_CHOICES:
            raise ValueError(f"Unknown embedder pin {chosen!r}; must be one of {sorted(_PIN_CHOICES)}")
        if chosen == "null":
            return NullEmbedder()
        if chosen == "openai":
            return OpenAIEmbedder()
        return LettaEmbedder()

    backend = resolve_memory_backend(root=default_store_root())
    if backend == "sqlite":
        return NullEmbedder()
    if backend == "letta":
        try:
            from lemoncrow.infra.memory_bridges.letta_adapter import LettaAdapter
        except ImportError:
            return NullEmbedder()
        if LettaAdapter.is_available():
            return LettaEmbedder()
        logger.warning("Letta backend selected but sidecar is unavailable; falling back to FTS-only (null embedder)")
        return NullEmbedder()
    if backend == "openmemory" and os.environ.get("OPENAI_API_KEY"):
        return OpenAIEmbedder()
    return NullEmbedder()


_embedder_singleton: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder_singleton
    if _embedder_singleton is None:
        _embedder_singleton = make_embedder()
    return _embedder_singleton


def embed_queries(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    if isinstance(embedder, TaskAwareEmbedder):
        return embedder.embed_queries(texts)
    return embedder.embed(texts)


def embed_documents(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    if isinstance(embedder, TaskAwareEmbedder):
        return embedder.embed_documents(texts)
    return embedder.embed(texts)


def _default_code_model(model: str | None = None) -> str:
    return (
        model or os.getenv("LEMONCROW_CODE_EMBED_MODEL") or DEFAULT_CODE_EMBED_MODEL
    ).strip() or DEFAULT_CODE_EMBED_MODEL


@lru_cache(maxsize=8)
def _cached_ollama_code_embedder(model: str) -> OllamaEmbedder:
    return OllamaEmbedder(model=model)


def _make_available_ollama_code_embedder(model: str) -> OllamaEmbedder:
    # Construction is cached, but availability is re-checked on every call so a
    # mid-session Ollama outage falls back to the local embedder instead of
    # returning a stale embedder whose embed() would hit a dead socket.
    embedder = _cached_ollama_code_embedder(model)
    if not embedder.is_available():
        raise RuntimeError(f"Ollama model {model!r} is unavailable")
    return embedder


def make_code_embedder(pin: str | None = None, model: str | None = None) -> Embedder:
    chosen = (pin or os.getenv("LEMONCROW_CODE_EMBEDDER") or os.getenv("LEMONCROW_EMBEDDER") or "").strip().lower()
    if chosen and chosen not in _CODE_PIN_CHOICES:
        raise ValueError(f"Unknown code embedder pin {chosen!r}; must be one of {sorted(_CODE_PIN_CHOICES)}")
    if chosen == "openai":
        return OpenAIEmbedder()
    if chosen == "letta":
        return LettaEmbedder()
    if chosen == "nomic":
        return NomicEmbedder(model or "nomic-ai/nomic-embed-code")
    if chosen == "hf":
        # Generic SentenceTransformer pin — model required via LEMONCROW_CODE_EMBED_MODEL.
        # Prefixes: LEMONCROW_HF_QUERY_PREFIX / LEMONCROW_HF_DOC_PREFIX (default: empty).
        from .nomic import NomicEmbedder as _ST  # same ST wrapper, model-agnostic

        hf_model = model or os.getenv("LEMONCROW_CODE_EMBED_MODEL", "")
        if not hf_model:
            raise ValueError("LEMONCROW_CODE_EMBED_MODEL must be set when LEMONCROW_CODE_EMBEDDER=hf")
        return _ST(
            hf_model,
            query_prefix=os.getenv("LEMONCROW_HF_QUERY_PREFIX", ""),
            doc_prefix=os.getenv("LEMONCROW_HF_DOC_PREFIX", ""),
            use_cache=True,  # use_cache kwarg currently unused; DynamicCache compat is handled in _load()
        )
    if chosen == "bge":
        return BgeEmbedder()
    if chosen == "ollama":
        if os.getenv("LEMONCROW_OFFLINE"):
            return NullEmbedder()
        try:
            return _make_available_ollama_code_embedder(_default_code_model(model))
        except RuntimeError:
            return NullEmbedder()
    if chosen == "null":
        return NullEmbedder()
    # No pin set: auto-select semantic search when the extras are installed,
    # per BgeEmbedder.is_available()'s documented contract and BENCHMARKS.md's
    # "Semantic Code Search Embedder Sweep" (BGE-Code-v1 has the best MRR).
    # GPUs below the VRAM threshold -- or no GPU at all -- get the much
    # smaller SFR-Embedding-Code-400M_R so indexing stays fast enough to be
    # usable. Neither extra installed: no semantic search (FTS-only).
    if BgeEmbedder.is_available():
        if _gpu_has_sufficient_vram_for_bge():
            return BgeEmbedder()
        from .nomic import NomicEmbedder as _ST  # same ST wrapper, model-agnostic

        sfr = _ST(_SFR_FALLBACK_MODEL, query_prefix="", doc_prefix="")
        sfr.dim = _SFR_FALLBACK_DIM
        sfr.name = f"sfr:{_SFR_FALLBACK_MODEL}"
        return sfr
    return NullEmbedder()


def get_code_embedder() -> Embedder:
    return make_code_embedder(
        pin=os.getenv("LEMONCROW_CODE_EMBEDDER") or os.getenv("LEMONCROW_EMBEDDER") or None,
        model=os.getenv("LEMONCROW_CODE_EMBED_MODEL") or None,
    )


def _clear_code_embedder_cache() -> None:
    _cached_ollama_code_embedder.cache_clear()


make_code_embedder.cache_clear = _clear_code_embedder_cache  # type: ignore[attr-defined]


__all__ = [
    "DEFAULT_CODE_EMBED_MODEL",
    "LettaEmbedder",
    "NomicEmbedder",
    "NullEmbedder",
    "OllamaEmbedder",
    "OpenAIEmbedder",
    "embed_documents",
    "embed_queries",
    "get_code_embedder",
    "get_embedder",
    "make_code_embedder",
    "make_embedder",
]
