"""Durable local deferred indexing queue and bounded worker.

I1 deliberately keys work by the identity available before RepositoryGeneration
lands: ``(org, repo, view, layer, revision)``. I3 replaces the View term with an
immutable generation identity; the queue semantics (dedupe, CAS publication,
priority and retry) remain the same.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol

from ..errors import ErrorCode, ServerError
from .contracts import IndexBackend, IndexLayerStatus, repository_generation_scope_id
from .layers import IndexBuildCancelled

__all__ = [
    "ACTIVE_PRIORITY",
    "RECOVERY_PRIORITY",
    "DeferredIndexJob",
    "DeferredIndexJobStore",
    "DeferredIndexWorker",
    "MemoryDeferredJobStore",
    "SqliteDeferredJobStore",
    "cancel_deferred_view",
    "enqueue_deferred_layer",
    "enqueue_deferred_view",
]

ACTIVE_PRIORITY: Final[int] = 0
RECOVERY_PRIORITY: Final[int] = 50
_SEMANTIC_OFFSET: Final[int] = 10
_LAYERS: Final[frozenset[str]] = frozenset({"link", "semantic"})

_JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS deferred_index_jobs (
    job_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    repo_id TEXT NOT NULL,
    view_id TEXT NOT NULL,
    target_kind TEXT NOT NULL DEFAULT 'view',
    target_key TEXT NOT NULL DEFAULT '',
    layer TEXT NOT NULL,
    revision INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    requested_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    not_before REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS deferred_index_jobs_claim
    ON deferred_index_jobs(state, not_before, priority, requested_at);
CREATE INDEX IF NOT EXISTS deferred_index_jobs_view
    ON deferred_index_jobs(view_id, layer, revision);
"""


def _job_id(
    org_id: str,
    repo_id: str,
    view_id: str,
    layer: str,
    revision: int,
    *,
    target_kind: str,
    target_key: str,
) -> str:
    # Preserve pre-I3 ids for mutable View jobs so a restart across the schema
    # upgrade cannot duplicate an already-pending job. Immutable generation
    # work instead keys on generation+possession scope and deliberately ignores
    # the representative View revision.
    if target_kind == "view" and target_key == view_id:
        payload = [org_id, repo_id, view_id, layer, revision]
    else:
        payload = [org_id, repo_id, target_kind, target_key, layer]
    return "idx_" + hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class DeferredIndexJob:
    job_id: str
    org_id: str
    repo_id: str
    view_id: str
    target_kind: str
    target_key: str
    layer: str
    revision: int
    priority: int
    state: str
    attempts: int
    requested_at: float
    updated_at: float
    not_before: float = 0.0
    error: str = ""


class DeferredIndexJobStore(Protocol):
    def enqueue(
        self,
        *,
        org_id: str,
        repo_id: str,
        view_id: str,
        target_kind: str = "view",
        target_key: str = "",
        layer: str,
        revision: int,
        priority: int,
        now: float,
        force: bool = False,
    ) -> DeferredIndexJob: ...

    def claim(self, now: float) -> DeferredIndexJob | None: ...
    def complete(self, job_id: str, now: float) -> None: ...
    def obsolete(self, job_id: str, now: float, reason: str = "") -> None: ...
    def retry(self, job_id: str, now: float, *, not_before: float, error: str) -> None: ...
    def fail(self, job_id: str, now: float, error: str) -> None: ...
    def cancel_view(self, view_id: str, now: float) -> None: ...
    def recover(self, now: float) -> int: ...
    def counts(self) -> tuple[int, int]: ...
    def wait(self, timeout: float) -> None: ...
    def wake(self) -> None: ...
    def get(self, job_id: str) -> DeferredIndexJob | None: ...


class MemoryDeferredJobStore:
    """Process-local queue for explicit ephemeral local-server mode."""

    def __init__(self) -> None:
        self._jobs: dict[str, DeferredIndexJob] = {}
        self._condition = threading.Condition()

    def enqueue(
        self,
        *,
        org_id: str,
        repo_id: str,
        view_id: str,
        target_kind: str = "view",
        target_key: str = "",
        layer: str,
        revision: int,
        priority: int,
        now: float,
        force: bool = False,
    ) -> DeferredIndexJob:
        if layer not in _LAYERS:
            raise ValueError(f"unknown deferred index layer: {layer}")
        if target_kind not in {"view", "generation"}:
            raise ValueError(f"unknown deferred index target: {target_kind}")
        target_key = target_key or view_id
        ident = _job_id(
            org_id,
            repo_id,
            view_id,
            layer,
            revision,
            target_kind=target_kind,
            target_key=target_key,
        )
        with self._condition:
            for key, job in tuple(self._jobs.items()):
                if (
                    job.target_kind == target_kind
                    and job.target_key == target_key
                    and job.layer == layer
                    and job.revision < revision
                    and job.state in {"completed", "obsolete", "failed"}
                ):
                    del self._jobs[key]
            for key, job in tuple(self._jobs.items()):
                if (
                    job.target_kind == target_kind
                    and job.target_key == target_key
                    and job.layer == layer
                    and job.revision < revision
                    and job.state in {"pending", "running"}
                ):
                    self._jobs[key] = replace(job, state="obsolete", updated_at=now, error="superseded")
            existing = self._jobs.get(ident)
            if existing is None:
                job = DeferredIndexJob(
                    ident,
                    org_id,
                    repo_id,
                    view_id,
                    target_kind,
                    target_key,
                    layer,
                    revision,
                    priority,
                    "pending",
                    0,
                    now,
                    now,
                )
            elif (
                existing.state == "completed"
                and not force
                and not (target_kind == "generation" and existing.view_id != view_id)
            ):
                job = replace(existing, priority=min(existing.priority, priority), updated_at=now)
            elif existing.state == "running" and not force:
                job = replace(existing, priority=min(existing.priority, priority), updated_at=now)
            else:
                job = replace(
                    existing,
                    view_id=view_id,
                    priority=min(existing.priority, priority),
                    state="pending",
                    updated_at=now,
                    not_before=0.0,
                    error="",
                )
            self._jobs[ident] = job
            self._condition.notify_all()
            return job

    def claim(self, now: float) -> DeferredIndexJob | None:
        with self._condition:
            eligible = [job for job in self._jobs.values() if job.state == "pending" and job.not_before <= now]
            if not eligible:
                return None
            job = min(eligible, key=lambda item: (item.priority, item.requested_at, item.job_id))
            claimed = replace(job, state="running", attempts=job.attempts + 1, updated_at=now)
            self._jobs[job.job_id] = claimed
            return claimed

    def _state(self, job_id: str, now: float, state: str, *, error: str = "", not_before: float = 0.0) -> None:
        with self._condition:
            job = self._jobs.get(job_id)
            if job is None:
                return
            self._jobs[job_id] = replace(job, state=state, updated_at=now, error=error[:2048], not_before=not_before)
            self._condition.notify_all()

    def complete(self, job_id: str, now: float) -> None:
        self._state(job_id, now, "completed")

    def obsolete(self, job_id: str, now: float, reason: str = "") -> None:
        self._state(job_id, now, "obsolete", error=reason)

    def retry(self, job_id: str, now: float, *, not_before: float, error: str) -> None:
        self._state(job_id, now, "pending", error=error, not_before=not_before)

    def fail(self, job_id: str, now: float, error: str) -> None:
        self._state(job_id, now, "failed", error=error)

    def cancel_view(self, view_id: str, now: float) -> None:
        with self._condition:
            for key, job in tuple(self._jobs.items()):
                if job.target_kind == "view" and job.view_id == view_id and job.state in {"pending", "running"}:
                    self._jobs[key] = replace(job, state="obsolete", updated_at=now, error="view_closed")
            self._condition.notify_all()

    def recover(self, now: float) -> int:
        recovered = 0
        with self._condition:
            for key, job in tuple(self._jobs.items()):
                if job.state == "running":
                    self._jobs[key] = replace(job, state="pending", updated_at=now, error="restart_recovery")
                    recovered += 1
            self._condition.notify_all()
        return recovered

    def counts(self) -> tuple[int, int]:
        with self._condition:
            queued = sum(job.state == "pending" for job in self._jobs.values())
            running = sum(job.state == "running" for job in self._jobs.values())
            return queued, running

    def wait(self, timeout: float) -> None:
        with self._condition:
            self._condition.wait(timeout=max(0.0, timeout))

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def get(self, job_id: str) -> DeferredIndexJob | None:
        with self._condition:
            return self._jobs.get(job_id)


class SqliteDeferredJobStore:
    """Durable queue living in the repository-router catalogue SQLite DB."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._condition = threading.Condition()
        with db._lock:
            db._conn.executescript(_JOB_SCHEMA)
            columns = {str(row[1]) for row in db._conn.execute("PRAGMA table_info(deferred_index_jobs)").fetchall()}
            if "target_kind" not in columns:
                db._conn.execute("ALTER TABLE deferred_index_jobs ADD COLUMN target_kind TEXT NOT NULL DEFAULT 'view'")
            if "target_key" not in columns:
                db._conn.execute("ALTER TABLE deferred_index_jobs ADD COLUMN target_key TEXT NOT NULL DEFAULT ''")
            db._conn.execute(
                "UPDATE deferred_index_jobs SET target_key=view_id WHERE target_key='' AND target_kind='view'"
            )
            db._conn.execute(
                "CREATE INDEX IF NOT EXISTS deferred_index_jobs_target "
                "ON deferred_index_jobs(target_kind, target_key, layer, revision)"
            )

    @staticmethod
    def _from_row(row: Any) -> DeferredIndexJob:
        return DeferredIndexJob(
            job_id=str(row["job_id"]),
            org_id=str(row["org_id"]),
            repo_id=str(row["repo_id"]),
            view_id=str(row["view_id"]),
            target_kind=str(row["target_kind"]),
            target_key=str(row["target_key"]),
            layer=str(row["layer"]),
            revision=int(row["revision"]),
            priority=int(row["priority"]),
            state=str(row["state"]),
            attempts=int(row["attempts"]),
            requested_at=float(row["requested_at"]),
            updated_at=float(row["updated_at"]),
            not_before=float(row["not_before"]),
            error=str(row["error"]),
        )

    def enqueue(
        self,
        *,
        org_id: str,
        repo_id: str,
        view_id: str,
        target_kind: str = "view",
        target_key: str = "",
        layer: str,
        revision: int,
        priority: int,
        now: float,
        force: bool = False,
    ) -> DeferredIndexJob:
        if layer not in _LAYERS:
            raise ValueError(f"unknown deferred index layer: {layer}")
        if target_kind not in {"view", "generation"}:
            raise ValueError(f"unknown deferred index target: {target_kind}")
        target_key = target_key or view_id
        ident = _job_id(
            org_id,
            repo_id,
            view_id,
            layer,
            revision,
            target_kind=target_kind,
            target_key=target_key,
        )
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM deferred_index_jobs WHERE target_kind=? AND target_key=? AND layer=? AND revision<? "
                "AND state IN ('completed','obsolete','failed')",
                (target_kind, target_key, layer, revision),
            )
            conn.execute(
                "UPDATE deferred_index_jobs SET state='obsolete', updated_at=?, error='superseded' "
                "WHERE target_kind=? AND target_key=? AND layer=? AND revision<? "
                "AND state IN ('pending','running')",
                (now, target_kind, target_key, layer, revision),
            )
            existing = conn.execute("SELECT * FROM deferred_index_jobs WHERE job_id=?", (ident,)).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO deferred_index_jobs "
                    "(job_id,org_id,repo_id,view_id,target_kind,target_key,layer,revision,priority,state,attempts,requested_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'pending',0,?,?)",
                    (
                        ident,
                        org_id,
                        repo_id,
                        view_id,
                        target_kind,
                        target_key,
                        layer,
                        revision,
                        priority,
                        now,
                        now,
                    ),
                )
            else:
                state = str(existing["state"])
                requeue_completed_generation = (
                    target_kind == "generation" and state == "completed" and str(existing["view_id"]) != view_id
                )
                if force or state not in {"completed", "running"} or requeue_completed_generation:
                    conn.execute(
                        "UPDATE deferred_index_jobs SET view_id=?, priority=MIN(priority,?), state='pending', updated_at=?, "
                        "not_before=0, error='' WHERE job_id=?",
                        (view_id, priority, now, ident),
                    )
                else:
                    conn.execute(
                        "UPDATE deferred_index_jobs SET priority=MIN(priority,?), updated_at=? WHERE job_id=?",
                        (priority, now, ident),
                    )
            row = conn.execute("SELECT * FROM deferred_index_jobs WHERE job_id=?", (ident,)).fetchone()
        assert row is not None
        self.wake()
        return self._from_row(row)

    def claim(self, now: float) -> DeferredIndexJob | None:
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM deferred_index_jobs WHERE state='pending' AND not_before<=? "
                "ORDER BY priority, requested_at, job_id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            job_id = str(row["job_id"])
            changed = conn.execute(
                "UPDATE deferred_index_jobs SET state='running', attempts=attempts+1, updated_at=? "
                "WHERE job_id=? AND state='pending'",
                (now, job_id),
            ).rowcount
            if changed != 1:
                return None
            claimed = conn.execute("SELECT * FROM deferred_index_jobs WHERE job_id=?", (job_id,)).fetchone()
        return None if claimed is None else self._from_row(claimed)

    def _state(self, job_id: str, now: float, state: str, *, error: str = "", not_before: float = 0.0) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE deferred_index_jobs SET state=?, updated_at=?, error=?, not_before=? WHERE job_id=?",
                (state, now, error[:2048], not_before, job_id),
            )
        self.wake()

    def complete(self, job_id: str, now: float) -> None:
        self._state(job_id, now, "completed")

    def obsolete(self, job_id: str, now: float, reason: str = "") -> None:
        self._state(job_id, now, "obsolete", error=reason)

    def retry(self, job_id: str, now: float, *, not_before: float, error: str) -> None:
        self._state(job_id, now, "pending", error=error, not_before=not_before)

    def fail(self, job_id: str, now: float, error: str) -> None:
        self._state(job_id, now, "failed", error=error)

    def cancel_view(self, view_id: str, now: float) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE deferred_index_jobs SET state='obsolete', updated_at=?, error='view_closed' "
                "WHERE target_kind='view' AND view_id=? AND state IN ('pending','running')",
                (now, view_id),
            )
        self.wake()

    def recover(self, now: float) -> int:
        with self._db.transaction() as conn:
            changed = conn.execute(
                "UPDATE deferred_index_jobs SET state='pending', updated_at=?, error='restart_recovery' "
                "WHERE state='running'",
                (now,),
            ).rowcount
        self.wake()
        return max(0, int(changed))

    def counts(self) -> tuple[int, int]:
        queued = self._db.scalar_int("SELECT COUNT(*) FROM deferred_index_jobs WHERE state='pending'")
        running = self._db.scalar_int("SELECT COUNT(*) FROM deferred_index_jobs WHERE state='running'")
        return queued, running

    def wait(self, timeout: float) -> None:
        with self._condition:
            self._condition.wait(timeout=max(0.0, timeout))

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def get(self, job_id: str) -> DeferredIndexJob | None:
        row = self._db.row("SELECT * FROM deferred_index_jobs WHERE job_id=?", (job_id,))
        return None if row is None else self._from_row(row)


def _store(backend: IndexBackend) -> DeferredIndexJobStore | None:
    store = backend.deferred_jobs
    return store if store is not None else None


def enqueue_deferred_layer(
    backend: IndexBackend,
    org_id: str,
    view_id: str,
    layer: str,
    *,
    priority: int = ACTIVE_PRIORITY,
    force: bool = False,
    now: float | None = None,
) -> DeferredIndexJob | None:
    store = _store(backend)
    if store is None or layer not in _LAYERS:
        return None
    state = backend.views.get(org_id, view_id)
    if not state.manifest_complete:
        return None
    overlay, tombstones = backend.views.overlay(org_id, view_id)
    if state.generation_id and not overlay and not tombstones:
        target_kind = "generation"
        target_key = repository_generation_scope_id(state.generation_id, state.content_subject)
        revision = 0
    else:
        target_kind = "view"
        target_key = view_id
        revision = state.view_revision
    effective_priority = priority + (_SEMANTIC_OFFSET if layer == "semantic" else 0)
    return store.enqueue(
        org_id=org_id,
        repo_id=state.repo_id,
        view_id=view_id,
        target_kind=target_kind,
        target_key=target_key,
        layer=layer,
        revision=revision,
        priority=effective_priority,
        now=time.time() if now is None else now,
        force=force,
    )


def enqueue_deferred_view(
    backend: IndexBackend,
    org_id: str,
    view_id: str,
    *,
    priority: int = ACTIVE_PRIORITY,
    force: bool = False,
    now: float | None = None,
) -> tuple[DeferredIndexJob, ...]:
    jobs = []
    for layer in ("link", "semantic"):
        job = enqueue_deferred_layer(backend, org_id, view_id, layer, priority=priority, force=force, now=now)
        if job is not None:
            jobs.append(job)
    return tuple(jobs)


def cancel_deferred_view(backend: IndexBackend, view_id: str, *, now: float | None = None) -> None:
    store = _store(backend)
    if store is not None:
        store.cancel_view(view_id, time.time() if now is None else now)


class DeferredIndexWorker:
    """One bounded local worker; durable queue state survives process restarts."""

    def __init__(
        self,
        backend: IndexBackend,
        *,
        max_files: int = 2_000_000,
        max_attempts: int = 5,
        retry_delay_s: float = 0.5,
        clock: Callable[[], float] = time.time,
    ) -> None:
        store = _store(backend)
        if store is None:
            raise ValueError("deferred indexing requires a job store")
        if max_files < 1 or max_attempts < 1 or retry_delay_s < 0:
            raise ValueError("invalid deferred indexing worker limits")
        self._backend = backend
        self._store = store
        self._max_files = max_files
        self._max_attempts = max_attempts
        self._retry_delay_s = retry_delay_s
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            now = float(self._clock())
            self._store.recover(now)
            self._enqueue_restart_views(now)
            thread = threading.Thread(target=self._run, name="lc-index-deferred", daemon=True)
            self._thread = thread
            thread.start()

    def close(self) -> None:
        self._stop.set()
        self._store.wake()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join()
        with self._lock:
            self._thread = None

    def _enqueue_restart_views(self, now: float) -> None:
        partitions = self._backend.partitions
        if partitions is None:
            return
        for leaf in partitions():
            key = leaf.partition_key
            if key is None:
                continue
            org_id, _repo_id = key
            for view_id in leaf.views.view_ids_for_org(org_id):
                try:
                    state = leaf.views.get(org_id, view_id)
                except ServerError:
                    continue
                if state.manifest_complete:
                    enqueue_deferred_view(
                        leaf,
                        org_id,
                        view_id,
                        priority=RECOVERY_PRIORITY,
                        force=True,
                        now=now,
                    )

    def _metrics(self) -> None:
        ops = self._backend.ops
        if ops is None:
            return
        queued, running = self._store.counts()
        ops.worker_queue(queued=queued, running=running)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._metrics()
            job = self._store.claim(float(self._clock()))
            if job is None:
                self._store.wait(0.25)
                continue
            self._metrics()
            self._process(job)
        self._metrics()

    def _resolve_job_view(self, leaf: IndexBackend, job: DeferredIndexJob) -> tuple[str, Any] | None:
        if job.target_kind == "view":
            try:
                state = leaf.views.get(job.org_id, job.view_id)
            except ServerError as exc:
                if exc.code is ErrorCode.VIEW_UNKNOWN:
                    return None
                raise
            return job.view_id, state

        # A generation job is not owned by the View that happened to enqueue it.
        # Prefer that View while it remains clean, then fall back to any other
        # live View with the same generation+possession scope. This is what lets
        # one worktree close without cancelling useful shared catch-up for nine
        # other worktrees over the same immutable base.
        ordered = [job.view_id]
        ordered.extend(view_id for view_id in leaf.views.view_ids_for_org(job.org_id) if view_id != job.view_id)
        for view_id in ordered:
            try:
                state = leaf.views.get(job.org_id, view_id)
            except ServerError as exc:
                if exc.code is ErrorCode.VIEW_UNKNOWN:
                    continue
                raise
            if not state.manifest_complete or not state.generation_id:
                continue
            overlay, tombstones = leaf.views.overlay(job.org_id, view_id)
            if overlay or tombstones:
                continue
            if repository_generation_scope_id(state.generation_id, state.content_subject) == job.target_key:
                return view_id, state
        return None

    def _process(self, job: DeferredIndexJob) -> None:
        started = time.perf_counter()
        outcome = "failed"
        try:
            leaf = self._backend.for_repository(job.org_id, job.repo_id)
            resolved = self._resolve_job_view(leaf, job)
            if resolved is None:
                self._store.obsolete(job.job_id, float(self._clock()), "target_gone")
                outcome = "obsolete"
                return
            active_view_id, current = resolved
            if job.target_kind == "view" and (current.view_revision != job.revision or not current.manifest_complete):
                self._store.obsolete(job.job_id, float(self._clock()), "revision_superseded")
                outcome = "obsolete"
                if current.manifest_complete:
                    enqueue_deferred_layer(
                        leaf,
                        job.org_id,
                        active_view_id,
                        job.layer,
                        priority=ACTIVE_PRIORITY,
                        now=float(self._clock()),
                    )
                return
            layer = leaf.links if job.layer == "link" else leaf.semantic
            last_cancel_check = [0.0]
            cancelled = [False]

            def should_cancel() -> bool:
                if self._stop.is_set():
                    return True
                now_mono = time.monotonic()
                if now_mono - last_cancel_check[0] < 0.05:
                    return cancelled[0]
                last_cancel_check[0] = now_mono
                current_job = self._store.get(job.job_id)
                cancelled[0] = current_job is None or current_job.state == "obsolete"
                return cancelled[0]

            status: IndexLayerStatus = layer.refresh(
                job.org_id,
                active_view_id,
                budget=self._max_files,
                wait=True,
                cancel=should_cancel,
            )
            if self._stop.is_set():
                outcome = "interrupted"
                return  # stays running; restart recovery returns it to pending
            latest = leaf.views.get(job.org_id, active_view_id)
            if job.target_kind == "view" and latest.view_revision != job.revision:
                self._store.obsolete(job.job_id, float(self._clock()), "revision_advanced_during_build")
                enqueue_deferred_layer(
                    leaf,
                    job.org_id,
                    active_view_id,
                    job.layer,
                    priority=ACTIVE_PRIORITY,
                    now=float(self._clock()),
                )
                outcome = "obsolete"
                return
            if job.target_kind == "generation":
                overlay, tombstones = leaf.views.overlay(job.org_id, active_view_id)
                still_target = (
                    latest.manifest_complete
                    and latest.generation_id
                    and not overlay
                    and not tombstones
                    and repository_generation_scope_id(latest.generation_id, latest.content_subject) == job.target_key
                )
                if not still_target:
                    if self._resolve_job_view(leaf, job) is None:
                        self._store.obsolete(job.job_id, float(self._clock()), "generation_no_longer_active")
                        outcome = "obsolete"
                    else:
                        self._retry(job, "generation_representative_changed")
                        outcome = "retry"
                    return
            expected_revision = latest.view_revision if job.target_kind == "generation" else job.revision
            if status.ready and status.at_view_revision == expected_revision:
                if job.target_kind == "generation" and leaf.publish_generation is not None:
                    # Publication requires every bundled derived layer. The
                    # first generation job to finish may therefore be a no-op;
                    # the later one publishes once link + semantic are ready.
                    leaf.publish_generation(job.org_id, active_view_id)
                self._store.complete(job.job_id, float(self._clock()))
                outcome = "completed"
            elif status.reason.endswith("_content_incomplete"):
                self._retry(job, status.reason)
                outcome = "retry"
            elif status.reason.endswith("_rebuild_deferred"):
                self._store.fail(job.job_id, float(self._clock()), f"background_budget:{self._max_files}")
                outcome = "capacity"
            else:
                self._retry(job, status.reason or status.state.value)
                outcome = "retry"
            ops = self._backend.ops
            if ops is not None:
                ops.layer(
                    layer=job.layer,
                    state=status.state.value,
                    revision_lag=max(0, latest.view_revision - status.at_view_revision),
                    covered_paths=status.covered_paths,
                )
        except IndexBuildCancelled:
            current_job = self._store.get(job.job_id)
            outcome = "obsolete" if current_job is None or current_job.state == "obsolete" else "interrupted"
        except Exception as exc:
            current_job = self._store.get(job.job_id)
            if current_job is None or current_job.state == "obsolete":
                outcome = "obsolete"
            else:
                self._retry(job, f"{type(exc).__name__}: {exc}")
                outcome = "retry" if job.attempts < self._max_attempts else "failed"
        finally:
            ops = self._backend.ops
            if ops is not None:
                ops.worker_outcome(layer=job.layer, outcome=outcome, duration_s=time.perf_counter() - started)
            self._metrics()

    def _retry(self, job: DeferredIndexJob, error: str) -> None:
        now = float(self._clock())
        if job.attempts >= self._max_attempts:
            self._store.fail(job.job_id, now, error)
            return
        delay = self._retry_delay_s * (2 ** max(0, job.attempts - 1))
        self._store.retry(job.job_id, now, not_before=now + delay, error=error)
