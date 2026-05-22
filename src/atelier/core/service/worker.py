"""Background worker for Atelier (P6).

The worker claims one job at a time from the store and dispatches it to a
registered handler.

Usage::

    from atelier.core.service.worker import Worker
    Worker(store).run()       # blocks forever (production)
    Worker(store).run_once()  # claim + process one job (useful in tests)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from atelier.core.foundation.paths import default_store_root
from atelier.core.service.jobs import JOB_BOOTSTRAP_CONTEXT, JOB_CONSOLIDATE_BLOCKS, KNOWN_JOB_TYPES

logger = logging.getLogger(__name__)

# Type alias for a job handler function.
JobHandler = Callable[[dict[str, Any]], dict[str, Any]]


class Worker:
    """Job worker.

    Args:
        store:    Any store object implementing the queue methods.
        dispatch: Override the handler registry (useful in tests).
        poll_interval: Seconds to sleep when the queue is empty.
    """

    def __init__(
        self,
        store: Any,
        *,
        dispatch: dict[str, JobHandler] | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        self._store = store
        self._poll_interval = poll_interval
        self._dispatch: dict[str, JobHandler] = dispatch if dispatch is not None else self._default_dispatch()

    def _default_dispatch(self) -> dict[str, JobHandler]:
        from atelier.core.capabilities.consolidation import consolidate
        from atelier.core.service.bootstrap_context import persist_bootstrap_plan
        from atelier.infra.storage.factory import make_memory_store

        def consolidate_handler(payload: dict[str, Any]) -> dict[str, Any]:
            report = consolidate(self._store, dry_run=bool(payload.get("dry_run", False)))
            return report.to_dict()

        def bootstrap_context_handler(payload: dict[str, Any]) -> dict[str, Any]:
            repo_root = Path(str(payload.get("repo_root", ""))).resolve()
            store_root = Path(getattr(self._store, "root", default_store_root())).resolve()
            result = persist_bootstrap_plan(
                repo_root,
                make_memory_store(store_root),
                actor="worker:bootstrap-context",
            )
            return result.model_dump(mode="json")

        return {
            JOB_CONSOLIDATE_BLOCKS: consolidate_handler,
            JOB_BOOTSTRAP_CONTEXT: bootstrap_context_handler,
        }

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Blocking event loop. Process jobs until interrupted."""
        logger.info("Atelier worker started (poll_interval=%ss)", self._poll_interval)
        while True:
            claimed = self.run_once()
            if claimed is None:
                time.sleep(self._poll_interval)

    def run_once(self) -> str | None:
        """Claim and process one job.

        Returns:
            The job ID that was processed, or *None* if the queue was empty.
        """
        job_row = self._store.claim_job()
        if job_row is None:
            return None

        job_id: str = job_row["id"]
        job_type: str = job_row["job_type"]
        payload: dict[str, Any] = job_row.get("payload") or {}
        logger.info("Processing job %s (type=%s)", job_id, job_type)

        if job_type not in KNOWN_JOB_TYPES:
            error = f"unknown job type: {job_type!r}"
            logger.error("Job %s failed: %s", job_id, error)
            self._store.fail_job(job_id, error)
            return job_id

        handler = self._dispatch.get(job_type)
        if handler is None:
            # Job type is known but no handler registered — treat as transient.
            error = f"no handler registered for job type: {job_type!r}"
            logger.warning("Job %s: %s", job_id, error)
            self._store.fail_job(job_id, error)
            return job_id

        try:
            result = handler(payload)
            self._store.complete_job(job_id, result)
            logger.info("Job %s completed successfully", job_id)
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error("Job %s failed: %s", job_id, error_msg)
            self._store.fail_job(job_id, error_msg)

        return job_id
