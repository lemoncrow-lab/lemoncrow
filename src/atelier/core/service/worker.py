"""Background worker for Atelier (P6).

The worker claims one job at a time from the store and dispatches it to a
registered handler. Optionally, it can also watch a directory for session
files and ingest them.

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
from atelier.core.service.ingest_session_directory import ingest_session_file
from atelier.core.service.jobs import (
    JOB_BOOTSTRAP_CONTEXT,
    JOB_CONSOLIDATE_BLOCKS,
    JOB_INGEST_SESSION_FILE,
    KNOWN_JOB_TYPES,
)

logger = logging.getLogger(__name__)

# Type alias for a job handler function.
JobHandler = Callable[[dict[str, Any]], dict[str, Any]]


class Worker:
    """Job worker.

    Args:
        store:    Any store object implementing the queue methods.
        dispatch: Override the handler registry (useful in tests).
        poll_interval: Seconds to sleep when the queue is empty.
        session_directory: Optional directory to watch for session files (JSONL).
                           If provided, the worker will check for new or modified
                           .jsonl files and ingest them.
    """

    def __init__(
        self,
        store: Any,
        *,
        dispatch: dict[str, JobHandler] | None = None,
        poll_interval: float = 5.0,
        session_directory: str | None = None,
    ) -> None:
        self._store = store
        self._poll_interval = poll_interval
        self._session_directory = session_directory
        self._dispatch: dict[str, JobHandler] = dispatch if dispatch is not None else self._default_dispatch()
        # Track seen session files and their modification times to avoid re-ingesting.
        self._seen_session_files: dict[Path, float] = {}
        if session_directory:
            self._session_directory_path = Path(session_directory)
            if not self._session_directory_path.exists() or not self._session_directory_path.is_dir():
                raise ValueError(f"Session directory does not exist or is not a directory: {session_directory}")

    def _default_dispatch(self) -> dict[str, JobHandler]:
        from atelier.core.capabilities.consolidation import consolidate
        from atelier.core.service.bootstrap_context import persist_bootstrap_plan
        from atelier.core.service.ingest_session import (
            ingest_session_file as ingest_session_file_service,
        )
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

        def ingest_session_handler(payload: dict[str, Any]) -> dict[str, Any]:
            file_path = payload.get("file_path")
            if not file_path:
                return {"status": "error", "message": "file_path is required"}
            return ingest_session_file_service(file_path, self._store)

        return {
            JOB_CONSOLIDATE_BLOCKS: consolidate_handler,
            JOB_BOOTSTRAP_CONTEXT: bootstrap_context_handler,
            JOB_INGEST_SESSION_FILE: ingest_session_handler,
        }

    # ------------------------------------------------------------------ #
    # Session directory watching                                          #
    # ------------------------------------------------------------------ #

    def _check_session_directory(self) -> None:
        """Check the session directory for new or modified .jsonl files and ingest them."""
        if not self._session_directory:
            return

        try:
            # Ensure the directory still exists (in case it was removed after init)
            if not self._session_directory_path.exists() or not self._session_directory_path.is_dir():
                logger.error("Session directory no longer exists: %s", self._session_directory_path)
                return

            # Scan for .jsonl files in the directory
            for file_path in self._session_directory_path.glob("*.jsonl"):
                try:
                    mtime = file_path.stat().st_mtime
                    # If we haven't seen this file or it's been modified
                    if file_path not in self._seen_session_files or self._seen_session_files[file_path] < mtime:
                        logger.info("Detected new or modified session file: %s", file_path)
                        result = ingest_session_file(str(file_path), self._store)
                        if result.get("status") == "success":
                            logger.info(
                                "Successfully ingested session file: %s (session_id: %s, events: %d)",
                                file_path,
                                result.get("session_id"),
                                result.get("event_count", 0),
                            )
                        else:
                            logger.error(
                                "Failed to ingest session file %s: %s",
                                file_path,
                                result.get("message", "Unknown error"),
                            )
                        self._seen_session_files[file_path] = mtime
                except OSError as exc:
                    logger.error("Error accessing file %s: %s", file_path, exc)

            # Remove entries for files that no longer exist
            self._seen_session_files = {
                path: mtime for path, mtime in self._seen_session_files.items() if path.exists()
            }
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Unexpected error while checking session directory: %s", exc)

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Blocking event loop. Process jobs until interrupted."""
        logger.info("Atelier worker started (poll_interval=%ss)", self._poll_interval)
        if self._session_directory:
            logger.info("Will also watch for session files in: %s", self._session_directory)
        while True:
            claimed = self.run_once()
            if claimed is None:
                # Queue was empty, so we can do directory watching if configured
                if self._session_directory:
                    self._check_session_directory()
                time.sleep(self._poll_interval)
            else:
                # We processed a job, so we can also check the directory (optional)
                # We'll check the directory after every job as well to be more responsive.
                if self._session_directory:
                    self._check_session_directory()

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
