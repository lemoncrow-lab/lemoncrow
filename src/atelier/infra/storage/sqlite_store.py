"""SQLiteStore — thin alias for atelier.core.store.ContextStore.

Provides:
  - the name ``SQLiteStore`` as the canonical export for the sqlite backend
  - a ``health_check`` method to satisfy StoreProtocol
  - no behaviour change for existing callers that use ContextStore directly
"""

from __future__ import annotations

import logging
from typing import Any

from atelier.core.foundation.store import ContextStore


class SQLiteStore(ContextStore):
    """SQLite-backed store (extends ContextStore with storage-layer helpers)."""

    def init(self) -> None:
        """Create core and V2 tables idempotently."""
        super().init()

    def health_check(self) -> dict[str, Any]:
        """Return basic health information."""
        try:
            with self._connect() as conn:
                count = conn.execute("SELECT COUNT(*) AS n FROM playbooks").fetchone()
                block_count = count["n"] if count else 0
            return {
                "ok": True,
                "backend": "sqlite",
                "db_path": str(self.db_path),
                "block_count": block_count,
            }
        except Exception as exc:
            logging.exception("Recovered from broad exception handler")
            return {"ok": False, "backend": "sqlite", "error": str(exc)}


__all__ = ["SQLiteStore"]
