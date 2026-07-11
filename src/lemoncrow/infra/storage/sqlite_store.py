"""SQLiteStore — thin alias for lemoncrow.core.store.ContextStore.

Provides:
  - the name ``SQLiteStore`` as the canonical export for the sqlite backend
  - a ``health_check`` method to satisfy StoreProtocol
  - jobs-table retention (``prune_jobs``) for the worker's retention_cleanup job
  - no behaviour change for existing callers that use ContextStore directly
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from lemoncrow.core.foundation.store import ContextStore


class SQLiteStore(ContextStore):
    """SQLite-backed store (extends ContextStore with storage-layer helpers)."""

    def init(self) -> None:
        """Create core and V2 tables idempotently."""
        super().init()

    def prune_jobs(self, *, older_than_days: int = 14) -> int:
        """Delete terminal jobs (succeeded/failed/dead) older than the cutoff.

        'failed' rows past the cutoff would have been retried long ago if any
        worker were running; keeping them only blocks the servicectl enqueue
        guard and grows the table forever.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=max(1, older_than_days))).isoformat()
        with self._transaction() as conn:
            res = conn.execute(
                "DELETE FROM jobs WHERE status IN ('succeeded', 'failed', 'dead') AND updated_at < ?",
                (cutoff,),
            )
        return int(res.rowcount or 0)

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
