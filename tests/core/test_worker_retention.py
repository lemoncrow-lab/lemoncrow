"""Retention cleanup reaches the review store, not only the job queue.

``ReviewStore.prune`` deletes ``archived`` reviews with their revisions, blobs
and evidence artifacts. It shipped with no caller, so the review store was a
durable thing that only ever grew. These cases pin the wiring: the scheduled
job is the closer, queue pruning happens first, and a review-store failure must
remain observable so the worker retries instead of recording false success.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.service.jobs import JOB_RETENTION_CLEANUP
from lemoncrow.core.service.worker import Worker
from lemoncrow.infra.storage.bundle import build_sqlite_store_bundle
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id


class _JobsStore:
    def __init__(self, root: Path) -> None:
        self.root = root


class _Store:
    """The queue half of a store bundle: a root, and a job pruner."""

    def __init__(self, root: Path) -> None:
        self.jobs = _JobsStore(root)
        self.pruned_jobs_days: int | None = None

    def prune_jobs(self, *, older_than_days: int = 14) -> int:
        self.pruned_jobs_days = older_than_days
        return 3


def _archived_review(root: Path) -> tuple[ReviewStore, str]:
    store = ReviewStore(root)
    store.init()
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root="/repo/alpha",
            range_mode="working_tree",
            title="uncommitted changes",
            source_ref="",
        )
    )
    store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="tree-aaa",
            packet_schema_version=1,
        ),
        [],
    )
    store.update_session(session.id, status="archived")
    return store, session.id


def _age(store: ReviewStore, review_id: str, *, days: int) -> None:
    """Backdate the row: the handler floors the cutoff at one day on purpose."""

    stamp = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE review_sessions SET updated_at = ? WHERE id = ?", (stamp, review_id))
        conn.commit()


def _run(store: _Store, payload: dict[str, Any]) -> dict[str, Any]:
    return Worker(store=store)._dispatch[JOB_RETENTION_CLEANUP](payload)  # type: ignore[arg-type]


def test_retention_cleanup_deletes_archived_reviews_past_the_cutoff(tmp_path: Path) -> None:
    root = tmp_path / "store"
    review_store, review_id = _archived_review(root)
    artifact, _digest, _size = review_store.write_packet_artifact(review_id, new_revision_id(), b"payload")
    assert (root / artifact).is_file()
    _age(review_store, review_id, days=120)

    result = _run(_Store(root), {"review_days": 90})

    assert result["status"] == "success"
    assert result["deleted_reviews"] == 1
    assert review_store.get_session(review_id) is None
    assert not (root / artifact).exists()


def test_retention_cleanup_spares_reviews_nobody_archived(tmp_path: Path) -> None:
    """Only an explicit human archive makes a review disposable."""

    root = tmp_path / "store"
    review_store, review_id = _archived_review(root)
    for status in ("open", "finished"):
        review_store.update_session(review_id, status=status)
        _age(review_store, review_id, days=120)
        result = _run(_Store(root), {"review_days": 90})
        assert result["deleted_reviews"] == 0, status
        assert review_store.get_session(review_id) is not None, status

    # ...and the same age with the archive flipped on is collected, which is
    # what proves the two cases above were spared for their status and not for
    # being too young.
    review_store.update_session(review_id, status="archived")
    _age(review_store, review_id, days=120)
    assert _run(_Store(root), {"review_days": 90})["deleted_reviews"] == 1


def test_retention_cleanup_fails_for_retry_after_job_prune_when_review_store_is_unusable(tmp_path: Path) -> None:
    """Cleanup failure is observable and retryable, after safe job-row pruning."""

    root = tmp_path / "store"
    root.mkdir(parents=True)
    # A directory where the database file belongs: opening it raises.
    (root / "lemoncrow_reviews.db").mkdir()

    store = _Store(root)
    with pytest.raises(RuntimeError, match="review retention cleanup failed"):
        _run(store, {"days": 7})

    # The independent queue cleanup already happened and is idempotent.
    assert store.pruned_jobs_days == 7


def test_worker_records_review_cleanup_failure_for_retry(tmp_path: Path) -> None:
    """The real queue row becomes failed, not succeeded-with-zero-deletions."""

    root = tmp_path / "store"
    bundle = build_sqlite_store_bundle(root)
    bundle.init()
    (root / "lemoncrow_reviews.db").mkdir()
    job_id = bundle.jobs.enqueue_job(JOB_RETENTION_CLEANUP, {"days": 7})

    assert Worker(bundle).run_once() == job_id

    rows = bundle.jobs.list_jobs(job_type=JOB_RETENTION_CLEANUP, limit=10)
    row = next(item for item in rows if item["id"] == job_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert "review retention cleanup failed" in str(row["error"])
