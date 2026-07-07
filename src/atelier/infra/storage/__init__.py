"""Data persistence layer (base classes and implementations)."""

from __future__ import annotations

from typing import Any

__all__ = ["ContextStore", "create_store", "make_memory_store"]


def __getattr__(name: str) -> Any:
    if name == "ContextStore":
        from atelier.core.foundation.store import ContextStore

        return ContextStore
    if name == "create_store":
        from atelier.infra.storage.factory import create_store

        return create_store
    if name == "make_memory_store":
        from atelier.infra.storage.factory import make_memory_store

        return make_memory_store
    raise AttributeError(name)
