"""JobsStore -- work queue and benchmark records.

Both ephemeral, worker-lifecycle tables. Benchmarks are grouped with the
queue because they could move to cold storage later and both are of a kind:
run once, read for a while, then pruned. The FK CASCADE inside benchmarks
(``benchmark_prompt_result.session_id -> benchmark_run.id``) stays intact
since both tables live in this one file.

Backed by ``lemoncrow_jobs.db``, physically separate from history,
knowledge, lessons, memory, and telemetry -- so ``servicectl tick`` (jobs +
history) never contends with knowledge reads or lesson review writes.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from lemoncrow.core.foundation.sqlite_base import SqliteTableStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    locked_by TEXT,
    locked_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_type_status ON jobs(job_type, status, created_at);

CREATE TABLE IF NOT EXISTS benchmark_run (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    suite TEXT NOT NULL,
    git_sha TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    n_prompts INTEGER NOT NULL DEFAULT 0,
    median_input_tokens_baseline INTEGER,
    median_input_tokens_optimized INTEGER,
    reduction_pct REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS benchmark_prompt_result (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES benchmark_run(id) ON DELETE CASCADE,
    prompt_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    input_tokens_baseline INTEGER NOT NULL,
    input_tokens_optimized INTEGER NOT NULL,
    reduction_pct REAL NOT NULL,
    duration_ms INTEGER NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    baseline_input_tokens INTEGER NOT NULL,
    optimized_input_tokens INTEGER NOT NULL,
    lever_attribution_json TEXT NOT NULL DEFAULT '{}'
);
"""


class JobsStore(SqliteTableStore):
    """SQLite-backed store for the job queue and benchmark records."""

    SCHEMA = SCHEMA
    REQUIRED_TABLES = ("jobs", "benchmark_run", "benchmark_prompt_result")

    def __init__(self, root: Path | str, *, db_name: str = "lemoncrow_jobs.db") -> None:
        super().__init__(root, db_name=db_name)

    def enqueue_job(
        self,
        job_type: str,
        payload: dict[str, Any] | None = None,
        *,
        max_attempts: int = 3,
    ) -> str:
        job_id = uuid4().hex
        now = datetime.now(UTC).isoformat()
        payload_json = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, job_type, payload, status, attempts, max_attempts,
                    locked_by, locked_at, error, created_at, updated_at
                )
                VALUES (?, ?, ?, 'pending', 0, ?, NULL, NULL, NULL, ?, ?)
                """,
                (job_id, job_type, payload_json, max_attempts, now, now),
            )
        return job_id

    def claim_job(self, worker_id: str | None = None) -> dict[str, Any] | None:
        claimed_by = worker_id or f"sqlite-{os.getpid()}"
        now = datetime.now(UTC).isoformat()
        lease_raw = os.environ.get("LEMONCROW_JOB_LEASE_SECONDS", "")
        lease_seconds = int(lease_raw) if lease_raw.isdigit() and int(lease_raw) > 0 else 900
        lease_cutoff = (datetime.now(UTC) - timedelta(seconds=lease_seconds)).isoformat()
        with self._transaction() as conn:
            if conn is not self._connection:
                # batch_mode's shared connection already owns an open transaction;
                # nesting BEGIN would raise and the batch owns the commit boundary.
                conn.execute("BEGIN IMMEDIATE")
            # Reap orphaned jobs before claiming. A worker that crashes mid-job
            # leaves its row stuck in 'running' forever (the lease is never
            # released), and because the servicectl enqueue guard treats
            # 'running'/'failed' as active, a single orphan blocks all future
            # enqueues of that job type indefinitely. Reclaim any 'running' job
            # whose lease expired so it retries, or dead-letters once attempts
            # are exhausted -- the queue self-heals instead of jamming.
            conn.execute(
                """
                UPDATE jobs
                SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'failed' END,
                    locked_by = NULL,
                    locked_at = NULL,
                    error = 'lease expired: worker did not finish (reaped)',
                    updated_at = ?
                WHERE status = 'running'
                  AND locked_at IS NOT NULL
                  AND locked_at < ?
                """,
                (now, lease_cutoff),
            )
            row = conn.execute("""
                SELECT *
                FROM jobs
                WHERE status IN ('pending', 'failed')
                  AND attempts < max_attempts
                ORDER BY created_at ASC
                LIMIT 1
                """).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    locked_by = ?,
                    locked_at = ?,
                    updated_at = ?,
                    error = NULL
                WHERE id = ?
                """,
                (claimed_by, now, now, row["id"]),
            )
            claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        return self._row_to_job(claimed) if claimed is not None else None

    def complete_job(self, job_id: str, result: dict[str, Any] | None = None) -> bool:
        _ = result
        now = datetime.now(UTC).isoformat()
        with self._transaction() as conn:
            res = conn.execute(
                """
                UPDATE jobs
                SET status = 'succeeded',
                    locked_by = NULL,
                    locked_at = NULL,
                    error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )
        return (res.rowcount or 0) > 0

    def fail_job(self, job_id: str, error: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._transaction() as conn:
            res = conn.execute(
                """
                UPDATE jobs
                SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'failed' END,
                    locked_by = NULL,
                    locked_at = NULL,
                    error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error, now, job_id),
            )
        return (res.rowcount or 0) > 0

    def list_jobs(
        self,
        *,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if job_type:
            sql += " AND job_type = ?"
            params.append(job_type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_job(row) for row in rows]

    def job_queue_health(self) -> dict[str, int]:
        lease_raw = os.environ.get("LEMONCROW_JOB_LEASE_SECONDS", "")
        lease_seconds = int(lease_raw) if lease_raw.isdigit() and int(lease_raw) > 0 else 900
        lease_cutoff = (datetime.now(UTC) - timedelta(seconds=lease_seconds)).isoformat()
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending,
                    COALESCE(SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END), 0) AS running,
                    COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed,
                    COALESCE(SUM(CASE WHEN status = 'dead' THEN 1 ELSE 0 END), 0) AS dead,
                    COALESCE(
                        SUM(
                            CASE
                                WHEN status = 'running' AND locked_at IS NOT NULL AND locked_at < ? THEN 1
                                ELSE 0
                            END
                        ),
                        0
                    ) AS stuck_running
                FROM jobs
                """,
                (lease_cutoff,),
            ).fetchone()
        pending = int(row["pending"]) if row is not None else 0
        running = int(row["running"]) if row is not None else 0
        failed = int(row["failed"]) if row is not None else 0
        dead = int(row["dead"]) if row is not None else 0
        stuck_running = int(row["stuck_running"]) if row is not None else 0
        return {
            "pending": pending,
            "running": running,
            "failed": failed,
            "dead": dead,
            "stuck_running": stuck_running,
            "active": pending + running + failed,
        }

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

    def _row_to_job(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = row["payload"]
        return {
            "id": row["id"],
            "job_type": row["job_type"],
            "payload": json.loads(payload) if isinstance(payload, str) else (payload or {}),
            "status": row["status"],
            "attempts": row["attempts"],
            "max_attempts": row["max_attempts"],
            "locked_by": row["locked_by"],
            "locked_at": row["locked_at"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


__all__ = ["JobsStore"]
