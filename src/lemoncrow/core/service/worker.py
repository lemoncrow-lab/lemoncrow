"""Background worker for LemonCrow (P6).

The worker claims one job at a time from the store and dispatches it to a
registered handler. Optionally, it can also watch a directory for session
files and ingest them.

Usage::

    from lemoncrow.core.service.worker import Worker
    Worker(store).run()       # blocks forever (production)
    Worker(store).run_once()  # claim + process one job (useful in tests)
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from lemoncrow.core.foundation.history_store import prune_raw_content
from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.core.service.ingest_session import ingest_session_file
from lemoncrow.core.service.jobs import (
    JOB_BACKGROUND_INTELLIGENCE,
    JOB_BOOTSTRAP_CONTEXT,
    JOB_CONSOLIDATE_BLOCKS,
    JOB_INGEST_SESSION_FILE,
    JOB_OPTIMIZE,
    JOB_RETENTION_CLEANUP,
    KNOWN_JOB_TYPES,
    JobPaused,
)
from lemoncrow.infra.storage.bundle import StoreBundle

# Terminal jobs (succeeded/failed/dead) older than this are pruned.
DEFAULT_JOB_RETENTION_DAYS = 14

# Archived reviews older than this are deleted with their revisions, blobs and
# evidence artifacts. Longer than the job cutoff on purpose: a review a human
# explicitly archived is a discarded record of somebody's work, not a spent
# queue row, and the artifacts make it the larger thing on disk.
DEFAULT_REVIEW_RETENTION_DAYS = 90

# Redacted copies of host transcripts under ``raw/`` older than this are deleted (the parsed traces stay).
# ``0`` keeps them all. See ``prune_raw_content`` for which sources are never touched.
DEFAULT_RAW_RETENTION_DAYS = 90

logger = logging.getLogger(__name__)

# Type alias for a job handler function.
JobHandler = Callable[[dict[str, Any]], dict[str, Any]]


def job_lease_seconds() -> float:
    """The job lease the stores reap at (``LEMONCROW_JOB_LEASE_SECONDS``, default 900)."""

    raw = os.environ.get("LEMONCROW_JOB_LEASE_SECONDS", "")
    return float(int(raw)) if raw.isdigit() and int(raw) > 0 else 900.0


@contextlib.contextmanager
def _held_lease(jobs: Any, job_id: str, worker_id: str, *, interval_s: float) -> Iterator[threading.Event]:
    """Heartbeat a claimed job's lease while its handler runs; the event is set once the lease is lost."""

    stop = threading.Event()
    lost = threading.Event()

    def beat() -> None:
        while not stop.wait(interval_s):
            try:
                if not jobs.heartbeat_job(job_id, locked_by=worker_id):
                    lost.set()
                    logger.warning("Job %s lost its lease while running (reaped or reclaimed)", job_id)
                    return
            except Exception:  # pylint: disable=broad-except
                # A transient store error: the next beat tries again before the lease expires.
                logger.warning("Job %s heartbeat failed", job_id, exc_info=True)

    thread = threading.Thread(target=beat, name=f"lemoncrow-job-lease-{job_id[:12]}", daemon=True)
    thread.start()
    try:
        yield lost
    finally:
        stop.set()
        thread.join(timeout=max(1.0, interval_s))


def _raw_retention_days(payload: dict[str, Any]) -> int:
    """``raw_days`` in the job payload, else ``LEMONCROW_RAW_RETENTION_DAYS``, else the default; ``0`` keeps everything."""

    for configured in (payload.get("raw_days"), os.environ.get("LEMONCROW_RAW_RETENTION_DAYS")):
        if configured in (None, ""):
            continue
        with contextlib.suppress(TypeError, ValueError):
            return max(0, int(configured))
    return DEFAULT_RAW_RETENTION_DAYS


def _prune_archived_reviews(store_root: Path, older_than_days: int) -> int:
    """Delete disposable reviews, surfacing cleanup failure to the worker.

    Job-row retention runs first. If the review store is locked, corrupt or from
    a newer incompatible schema, raising here makes the maintenance job fail and
    enter the worker's normal retry path instead of recording false success with
    ``deleted_reviews=0``. The already-completed job-row prune is idempotent.
    """

    try:
        from lemoncrow.pro.capabilities.review.store import ReviewStore

        return int(ReviewStore(store_root).prune(older_than_days=older_than_days))
    except Exception as exc:
        raise RuntimeError(f"review retention cleanup failed: {type(exc).__name__}: {exc}") from exc


def _expire_run_artifacts(store_root: Path) -> int:
    """Closer for raw Review run artifacts past their window and unreferenced."""

    try:
        from lemoncrow.pro.capabilities.review.run_evidence import RunArtifactStore, review_reference_resolver
        from lemoncrow.pro.capabilities.review.store import ReviewStore

        resolver = review_reference_resolver(ReviewStore(store_root))
        return len(RunArtifactStore.for_store(store_root).expire(resolver=resolver))
    except Exception as exc:
        raise RuntimeError(f"run artifact retention failed: {type(exc).__name__}: {exc}") from exc


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
        store: StoreBundle,
        *,
        dispatch: dict[str, JobHandler] | None = None,
        poll_interval: float = 5.0,
        session_directory: str | None = None,
    ) -> None:
        self._store = store
        self._poll_interval = poll_interval
        self._session_directory = session_directory
        # A stable worker prefix; run_once adds a fresh owner token per attempt.
        self._worker_id = f"worker-{os.getpid()}-{uuid4().hex[:12]}"
        self._dispatch: dict[str, JobHandler] = dispatch if dispatch is not None else self._default_dispatch()
        # Track seen session files and their modification times to avoid re-ingesting.
        self._seen_session_files: dict[Path, float] = {}
        if session_directory:
            self._session_directory_path = Path(session_directory)
            if not self._session_directory_path.exists() or not self._session_directory_path.is_dir():
                raise ValueError(f"Session directory does not exist or is not a directory: {session_directory}")

    def _default_dispatch(self) -> dict[str, JobHandler]:
        from lemoncrow.core.service.bootstrap_context import persist_bootstrap_plan
        from lemoncrow.core.service.ingest_session import (
            ingest_session_file as ingest_session_file_service,
        )
        from lemoncrow.infra.storage.factory import make_memory_store
        from lemoncrow.pro.capabilities.consolidation import consolidate
        from lemoncrow.pro.capabilities.optimization import run_optimization_cycle

        def consolidate_handler(payload: dict[str, Any]) -> dict[str, Any]:
            report = consolidate(self._store, dry_run=bool(payload.get("dry_run", False)))
            return report.to_dict()

        def bootstrap_context_handler(payload: dict[str, Any]) -> dict[str, Any]:
            repo_root = Path(str(payload.get("repo_root", ""))).resolve()
            store_root = Path(
                getattr(getattr(self._store, "jobs", None), "root", None) or default_store_root()
            ).resolve()
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

        def optimize_handler(payload: dict[str, Any]) -> dict[str, Any]:
            store_root = Path(
                getattr(getattr(self._store, "jobs", None), "root", None) or default_store_root()
            ).resolve()
            host_raw = payload.get("host")
            host = str(host_raw).strip() or None if host_raw is not None else None
            days = int(payload.get("days", 7) or 7)
            return run_optimization_cycle(
                store_root=store_root,
                host=host,
                days=max(1, days),
                source=str(payload.get("source", "worker")),
                open_pr=False,
                dry_run=False,
                proposal_tokens_threshold=None,
                benchmark_evidence=None,
                store=self._store,
            )

        def retention_cleanup_handler(payload: dict[str, Any]) -> dict[str, Any]:
            days = int(payload.get("days", DEFAULT_JOB_RETENTION_DAYS) or DEFAULT_JOB_RETENTION_DAYS)
            review_days = int(
                payload.get("review_days", DEFAULT_REVIEW_RETENTION_DAYS) or DEFAULT_REVIEW_RETENTION_DAYS
            )
            store_root = Path(
                getattr(getattr(self._store, "jobs", None), "root", None) or default_store_root()
            ).resolve()
            prune = getattr(self._store, "prune_jobs", None)
            if not callable(prune):
                return {"status": "skipped", "reason": "store does not support prune_jobs"}
            deleted = int(prune(older_than_days=max(1, days)))
            # The review store is the other durable thing this host accumulates.
            # Job rows are pruned first; if review cleanup then fails, the handler
            # raises so the worker records a failed maintenance job and retries
            # rather than treating "could not inspect the review store" as zero.
            deleted_reviews = _prune_archived_reviews(store_root, max(1, review_days))
            raw_files, raw_bytes = prune_raw_content(store_root / "raw", older_than_days=_raw_retention_days(payload))
            return {
                "status": "success",
                "deleted_jobs": deleted,
                "deleted_reviews": deleted_reviews,
                "expired_run_artifacts": _expire_run_artifacts(store_root),
                "pruned_raw_files": raw_files,
                "pruned_raw_bytes": raw_bytes,
            }

        def background_intelligence_handler(payload: dict[str, Any]) -> dict[str, Any]:
            from lemoncrow.pro.capabilities.review.background_local import LocalBackgroundScheduler

            store_root = Path(
                getattr(getattr(self._store, "jobs", None), "root", None) or default_store_root()
            ).resolve()
            return LocalBackgroundScheduler.for_root(self._store.jobs, store_root).run_job(payload)

        return {
            JOB_BACKGROUND_INTELLIGENCE: background_intelligence_handler,
            JOB_CONSOLIDATE_BLOCKS: consolidate_handler,
            JOB_BOOTSTRAP_CONTEXT: bootstrap_context_handler,
            JOB_INGEST_SESSION_FILE: ingest_session_handler,
            JOB_OPTIMIZE: optimize_handler,
            JOB_RETENTION_CLEANUP: retention_cleanup_handler,
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
                        status = result.get("status")
                        if status == "success":
                            logger.info(
                                "Successfully ingested session file: %s (session_id: %s, events: %d)",
                                file_path,
                                result.get("session_id"),
                                result.get("event_count", 0),
                            )
                            self._seen_session_files[file_path] = mtime
                        elif status == "skipped":
                            # Nothing was persisted, but retrying will not help.
                            logger.warning(
                                "Skipped session file %s: %s",
                                file_path,
                                result.get("reason", result.get("message", "not implemented")),
                            )
                            self._seen_session_files[file_path] = mtime
                        else:
                            # Do NOT record the mtime: a failed ingest is
                            # retried on the next poll instead of being
                            # silently marked as seen forever.
                            logger.error(
                                "Failed to ingest session file %s: %s",
                                file_path,
                                result.get("message", "Unknown error"),
                            )
                except OSError as exc:
                    logger.error("Error accessing file %s: %s", file_path, exc)

            # Remove entries for files that no longer exist
            self._seen_session_files = {
                path: mtime for path, mtime in self._seen_session_files.items() if path.exists()
            }
        except Exception as exc:  # pylint: disable=broad-except
            logging.exception("Recovered from broad exception handler")
            logger.error("Unexpected error while checking session directory: %s", exc)

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Blocking event loop. Process jobs until interrupted."""
        logger.info("LemonCrow worker started (poll_interval=%ss)", self._poll_interval)
        if self._session_directory:
            logger.info("Will also watch for session files in: %s", self._session_directory)
        consecutive_failures = 0
        while True:
            try:
                claimed = self.run_once()
            except Exception:
                # claim_job (store/DB) blew up: back off and retry instead of
                # letting one transient error kill the whole worker loop.
                consecutive_failures += 1
                delay = min(self._poll_interval * (2 ** min(consecutive_failures, 6)), 300.0)
                logger.exception("worker loop error (attempt %d); retrying in %.0fs", consecutive_failures, delay)
                time.sleep(delay)
                continue
            consecutive_failures = 0
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
        jobs = self._store.jobs
        # Fence each attempt, including concurrent/reclaimed runs by this same worker.
        worker_id = f"{self._worker_id}-{uuid4().hex}"
        job_row = jobs.claim_job(worker_id)
        if job_row is None:
            return None

        job_id: str = job_row["id"]
        job_type: str = job_row["job_type"]
        payload: dict[str, Any] = job_row.get("payload") or {}
        logger.info("Processing job %s (type=%s)", job_id, job_type)

        if job_type not in KNOWN_JOB_TYPES:
            error = f"unknown job type: {job_type!r}"
            jobs.fail_job(job_id, error, locked_by=worker_id)
            return job_id

        handler = self._dispatch.get(job_type)
        if handler is None:
            # Job type is known but no handler registered — treat as transient.
            error = f"no handler registered for job type: {job_type!r}"
            logger.warning("Job %s: %s", job_id, error)
            jobs.fail_job(job_id, error, locked_by=worker_id)
            return job_id

        # The lease is extended while the handler runs, so a long job is never
        # reaped (and re-run) under a live worker; every outcome below is fenced
        # on still holding it, so a worker whose lease was lost changes nothing.
        result: dict[str, Any] | None = None
        paused_reason: str | None = None
        error_msg: str | None = None
        with _held_lease(jobs, job_id, worker_id, interval_s=job_lease_seconds() / 3):
            try:
                result = handler(payload)
            except JobPaused as paused:
                paused_reason = paused.reason
            except Exception as exc:
                logging.exception("Recovered from broad exception handler")
                error_msg = f"{type(exc).__name__}: {exc}"
        if paused_reason is not None:
            # Not a failure: park it without spending the attempt; the maintenance tick resumes it.
            logger.info("Job %s paused: %s", job_id, paused_reason)
            outcome = jobs.pause_job(job_id, f"paused: {paused_reason}", locked_by=worker_id)
        elif error_msg is not None:
            logger.error("Job %s failed: %s", job_id, error_msg)
            outcome = jobs.fail_job(job_id, error_msg, locked_by=worker_id)
        else:
            outcome = jobs.complete_job(job_id, result, locked_by=worker_id)
            if outcome:
                logger.info("Job %s completed successfully", job_id)
        if not outcome:
            logger.warning("Job %s finished after its lease was lost; its outcome was not recorded", job_id)
        return job_id
