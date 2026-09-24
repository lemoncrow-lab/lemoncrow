"""Shared process cache for SemanticFileMemoryCapability instances."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.semantic_file_memory import SemanticFileMemoryCapability

semantic_file_memory_cache: dict[str, SemanticFileMemoryCapability] = {}
semantic_file_memory_lock = threading.Lock()


def semantic_file_memory(root: Path) -> SemanticFileMemoryCapability:
    """Return one cached semantic-file-memory capability per LemonCrow root."""
    from lemoncrow.pro.capabilities.semantic_file_memory import SemanticFileMemoryCapability

    key = str(root)
    cap = semantic_file_memory_cache.get(key)
    if cap is None:
        with semantic_file_memory_lock:
            cap = semantic_file_memory_cache.get(key)
            if cap is None:
                cap = SemanticFileMemoryCapability(root)
                semantic_file_memory_cache[key] = cap
    return cap


__all__ = ["semantic_file_memory", "semantic_file_memory_cache", "semantic_file_memory_lock"]
