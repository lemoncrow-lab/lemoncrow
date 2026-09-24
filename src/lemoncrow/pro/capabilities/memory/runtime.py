"""Process-local construction/cache for LemonCrow's host-neutral memory service."""

from __future__ import annotations

import threading
from pathlib import Path

from lemoncrow_client.kit.redaction import redact

from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.infra.embeddings.factory import make_embedder
from lemoncrow.infra.storage.factory import make_memory_store
from lemoncrow.infra.storage.memory_store import MemoryStore
from lemoncrow.pro.capabilities.memory.service import MemoryService

_memory_store_tls = threading.local()


def memory_store(root: str | Path | None = None) -> MemoryStore:
    """Return the per-thread memory store for *root*, reusing SQLite connections."""
    resolved = Path(root).expanduser().resolve() if root is not None else default_store_root()
    cache = getattr(_memory_store_tls, "by_root", None)
    if cache is None:
        cache = {}
        _memory_store_tls.by_root = cache
    key = str(resolved)
    store = cache.get(key)
    if store is None:
        store = make_memory_store(resolved)
        cache[key] = store
    return store


def memory_service(
    root: str | Path | None = None,
    *,
    store: MemoryStore | None = None,
) -> MemoryService:
    """Build the canonical memory service over *store* or the cached store for *root*."""
    resolved_store = store if store is not None else memory_store(root)
    return MemoryService(store=resolved_store, embedder=make_embedder(), redactor=redact)


__all__ = ["memory_service", "memory_store"]
