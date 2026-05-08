"""Storage factory functions."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

from atelier.core.foundation.paths import default_store_root
from atelier.infra.storage.memory_store import MemoryStore

if TYPE_CHECKING:
    from atelier.infra.storage.memory_store import MemoryStore

logger = logging.getLogger(__name__)


def create_store(root: Path) -> Any:
    """Create the configured storage backend for the given root path."""
    backend = os.environ.get("ATELIER_STORAGE_BACKEND", "sqlite").strip().lower() or "sqlite"
    resolved_root = Path(root)
    if backend == "sqlite":
        from atelier.infra.storage.sqlite_store import SQLiteStore

        return SQLiteStore(resolved_root)
    if backend == "postgres":
        from atelier.infra.storage.postgres_store import PostgresStore

        return PostgresStore(database_url=os.environ.get("ATELIER_DATABASE_URL", ""))
    raise ValueError("ATELIER_STORAGE_BACKEND must be 'sqlite' or 'postgres'")


def make_memory_store(root: str | Path | None, *, prefer: str | None = None) -> MemoryStore:
    """Create exactly one configured MemoryStore implementation."""
    raw_root: str | Path = root if root is not None else default_store_root()
    resolved_root = Path(raw_root)
    backend = _memory_backend(resolved_root, prefer=prefer)
    logger.info("selected memory backend: %s", backend)
    if backend == "letta":
        from atelier.infra.memory_bridges.letta_adapter import LettaMemoryStore

        return LettaMemoryStore(resolved_root)
    if backend == "openmemory":
        from atelier.infra.memory_bridges.openmemory import OpenMemoryMemoryStore

        return OpenMemoryMemoryStore(resolved_root)
    if backend != "sqlite":
        raise ValueError("memory backend must be 'sqlite', 'letta', or 'openmemory'")
    from atelier.infra.storage.sqlite_memory_store import SqliteMemoryStore

    return SqliteMemoryStore(resolved_root)


def _memory_backend(root: Path, *, prefer: str | None) -> str:
    env_backend = os.environ.get("ATELIER_MEMORY_BACKEND", "").strip().lower()
    if env_backend:
        return env_backend
    config_path = root / "config.toml"
    if config_path.exists() and tomllib is not None:
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
            memory = data.get("memory", {}) if isinstance(data, dict) else {}
            backend = str(memory.get("backend", "")).strip().lower()
            if backend:
                return backend
        except Exception as exc:
            logger.warning("failed to read memory backend config from %s: %s", config_path, exc)
    return (prefer or "sqlite").strip().lower()


__all__ = ["create_store", "make_memory_store"]
