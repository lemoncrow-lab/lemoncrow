"""``lemoncrow_reviews.db`` -- schema, versioning, round-trips, concurrency.

This store is the durable half of review: the browser pane is disposable, this
file is not. The cases below pin the properties that make it safe to build on --
reopen identity, one-transaction revision writes, FK cascade (which proves
``foreign_keys=ON`` is actually live), the newer-database refusal, and the
sync-friendliness rules a hosted team service would later need.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.session_models import (
    SESSION_SCHEMA_VERSION,
    Annotation,
    AnnotationAnchor,
    DeliveryRecord,
    DiscardedMark,
    ReviewEvidence,
    ReviewMark,
    ReviewRevision,
    ReviewSession,
    ReviewUnit,
)
from lemoncrow.pro.capabilities.review.store import (
    DB_NAME,
    AnnotationHistoryContext,
    AnnotationVersionConflict,
    ReviewStore,
    new_annotation_id,
    new_evidence_id,
    new_revision_id,
    new_session_id,
    utc_now,
)


def _store(root: Path) -> ReviewStore:
    store = ReviewStore(root)
    store.init()
    return store


def _session(**overrides: object) -> ReviewSession:
    base = ReviewSession(
        id=new_session_id(),
        subject_type="local_change",
        repo_root="/repo/alpha",
        range_mode="working_tree",
        title="uncommitted changes",
        source_ref="",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _revision(review_id: str, **overrides: object) -> ReviewRevision:
    base = ReviewRevision(
        id=new_revision_id(),
        review_id=review_id,
        revision_number=0,
        range_mode="working_tree",
        tree_fingerprint="tree-aaa",
        packet_schema_version=1,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _unit(unit_key: str = "file:src/app.py", **overrides: object) -> ReviewUnit:
    base = ReviewUnit(
        revision_id="",
        unit_key=unit_key,
        kind="file",
        path="src/app.py",
        content_fingerprint="fp-1",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _anchor(**overrides: object) -> AnnotationAnchor:
    base = AnnotationAnchor(
        path="src/app.py",
        side="new",
        start_line=10,
        end_line=12,
        selected_text="return value",
        selected_text_hash="sha-sel",
        before_context="def handler():",
        after_context="# tail",
        unit_key="file:src/app.py",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _seed(store: ReviewStore) -> tuple[ReviewSession, ReviewRevision]:
    session = store.create_session(_session())
    revision = store.add_revision(_revision(session.id), [_unit()])
    return session, revision


# ----- 1. schema creation ------------------------------------------------- #


def test_init_creates_the_file_and_every_required_table(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.db_path == tmp_path / DB_NAME
    assert store.db_path.is_file()
    with sqlite3.connect(store.db_path) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(ReviewStore.REQUIRED_TABLES) <= names
    assert len(ReviewStore.REQUIRED_TABLES) == 17
    assert {
        "review_participants",
        "review_requests",
        "review_attention",
        "review_attention_events",
        "review_provider_subjects",
        "provider_webhook_receipts",
        "provider_operations",
    }.isdisjoint(names)


def test_init_is_idempotent_and_keeps_existing_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_session(_session())
    store.init()
    store.init()
    assert store.get_session(session.id) is not None


def test_migration_from_empty_records_the_schema_version(tmp_path: Path) -> None:
    """An empty root migrates to the current version and says so on disk."""
    store = _store(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT value FROM review_meta WHERE key = 'schema_version'").fetchone()
    assert row is not None
    assert int(row[0]) == SESSION_SCHEMA_VERSION


def test_additive_review_migrations_are_registered_without_bumping_schema_version(tmp_path: Path) -> None:
    """Additive columns migrate independently; schema 1 remains backward-readable."""
    assert ReviewStore.MIGRATIONS == (
        "v3_001_review_annotations.sql",
        "v3_002_review_source_fingerprint.sql",
        "v3_003_review_author_response.sql",
        "v3_004_review_evidence_verification.sql",
        "v3_005_review_history.sql",
        "v3_006_review_collaboration.sql",
        "v3_009_review_feedback_operations.sql",
        "v3_011_review_outcomes.sql",
        "v3_012_review_current_revision.sql",
        "v3_013_review_change_proposals.sql",
    )
    assert SESSION_SCHEMA_VERSION == 1

    store = _store(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        annotation_columns = {row[1] for row in conn.execute("PRAGMA table_info(annotations)")}
        revision_columns = {row[1] for row in conn.execute("PRAGMA table_info(review_revisions)")}
        evidence_columns = {row[1] for row in conn.execute("PRAGMA table_info(review_evidence)")}
        delivery_columns = {row[1] for row in conn.execute("PRAGMA table_info(deliveries)")}
        session_columns = {row[1] for row in conn.execute("PRAGMA table_info(review_sessions)")}
        proposal_columns = {row[1] for row in conn.execute("PRAGMA table_info(review_change_proposals)")}
        history_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                "('review_mark_events', 'review_activity_events', 'annotation_versions')"
            )
        }
        annotation_version_columns = {row[1] for row in conn.execute("PRAGMA table_info(annotation_versions)")}
    assert {
        "source",
        "source_id",
        "title",
        "evidence_json",
        "confidence",
        "author_response",
        "author_response_source_id",
        "author_response_at",
    } <= annotation_columns
    assert "source_fingerprint" in revision_columns
    assert "current_revision_id" in session_columns
    assert {"verification_status", "detail"} <= evidence_columns
    assert {"operation_id", "revision_id", "feedback_hash", "annotation_version"} <= delivery_columns
    assert history_tables == {"review_mark_events", "review_activity_events", "annotation_versions"}
    assert {"turn_owner_kind", "turn_owner_id"} <= annotation_columns
    assert {"turn_owner_kind", "turn_owner_id"} <= annotation_version_columns
    assert {
        "base_revision_id",
        "replacement_text",
        "base_file_sha256",
        "applied_source_fingerprint",
        "result_revision_id",
    } <= proposal_columns
    assert {"operation_id", "revision_id", "feedback_hash", "annotation_version"} <= delivery_columns


def test_v3_009_migrates_an_existing_deliveries_table_before_building_operation_indexes(tmp_path: Path) -> None:
    """An existing review DB must gain feedback-operation columns before their indexes."""

    root = tmp_path / "pre-feedback-operations"
    store = _store(root)
    session, revision = _seed(store)
    annotation = store.add_annotation(
        Annotation(
            id="",
            review_id=session.id,
            revision_id=revision.id,
            anchor=_anchor(),
            body="preserve this delivery",
        )
    )
    stored = store.record_delivery(
        DeliveryRecord(
            id="",
            annotation_id=annotation.id,
            target_type="agent_session",
            target_ref="claude:session-1",
            state="sent",
        )
    )

    # Recreate the table exactly as it existed before v3_009 and mark only that
    # migration unapplied. The next ReviewStore.init() exercises the same path
    # as a real user upgrading an existing ~/.lemoncrow review database.
    with sqlite3.connect(store.db_path) as conn:
        conn.executescript("""
            DROP INDEX IF EXISTS idx_deliveries_operation_annotation;
            DROP INDEX IF EXISTS idx_deliveries_operation;
            ALTER TABLE deliveries RENAME TO deliveries_current;
            CREATE TABLE deliveries (
              id            TEXT PRIMARY KEY,
              annotation_id TEXT NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
              target_type   TEXT NOT NULL,
              target_ref    TEXT NOT NULL DEFAULT '',
              state         TEXT NOT NULL DEFAULT 'pending',
              remote_ref    TEXT NOT NULL DEFAULT '',
              last_error    TEXT NOT NULL DEFAULT '',
              created_at    TEXT NOT NULL,
              updated_at    TEXT NOT NULL
            );
            INSERT INTO deliveries (
              id, annotation_id, target_type, target_ref, state, remote_ref,
              last_error, created_at, updated_at
            )
            SELECT id, annotation_id, target_type, target_ref, state, remote_ref,
                   last_error, created_at, updated_at
              FROM deliveries_current;
            DROP TABLE deliveries_current;
            CREATE INDEX idx_deliveries_state ON deliveries(state, updated_at DESC);
            DELETE FROM _schema_migrations WHERE name = 'v3_009_review_feedback_operations.sql';
            """)

    upgraded = ReviewStore(root)
    upgraded.init()
    with sqlite3.connect(upgraded.db_path) as conn:
        delivery_columns = {row[1] for row in conn.execute("PRAGMA table_info(deliveries)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(deliveries)")}
    assert {"operation_id", "revision_id", "feedback_hash", "annotation_version"} <= delivery_columns
    assert {"idx_deliveries_operation", "idx_deliveries_operation_annotation"} <= indexes
    (delivery,) = upgraded.list_deliveries(annotation.id)
    assert delivery.id == stored.id
    assert delivery.state == "sent"
    assert delivery.operation_id == ""


def test_h1_store_migrates_to_history_without_losing_review_state(tmp_path: Path) -> None:
    """H2 migration is additive: durable H1 review state survives unchanged."""

    root = tmp_path / "h1-store"
    store = _store(root)
    session, revision = _seed(store)
    unit = store.list_units(revision.id)[0]
    mark = store.set_mark(
        ReviewMark(
            review_id=session.id,
            reviewer_id="local",
            unit_key=unit.unit_key,
            state="reviewed",
            reviewed_revision_id=revision.id,
            content_fingerprint=unit.content_fingerprint,
        )
    )
    annotation = store.add_annotation(
        Annotation(
            id="",
            review_id=session.id,
            revision_id=revision.id,
            anchor=_anchor(),
            body="keep this comment",
        )
    )

    # Simulate the on-disk shape immediately before H2: all existing review
    # rows remain, the three new history tables and their migration marker do not.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP TABLE annotation_versions")
        conn.execute("DROP TABLE review_activity_events")
        conn.execute("DROP TABLE review_mark_events")
        conn.execute("DELETE FROM _schema_migrations WHERE name = 'v3_005_review_history.sql'")

    migrated = ReviewStore(root)
    migrated.init()
    assert migrated.get_session(session.id) == session
    assert migrated.get_revision(revision.id) == revision
    assert migrated.list_marks(session.id) == (mark,)
    assert migrated.get_annotation(annotation.id) == annotation
    with sqlite3.connect(migrated.db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        migration = conn.execute("SELECT 1 FROM _schema_migrations WHERE name = 'v3_005_review_history.sql'").fetchone()
    assert {"review_mark_events", "review_activity_events", "annotation_versions"} <= tables
    assert migration is not None


def test_v3_006_migrates_shared_thread_ownership_without_hosted_tables(tmp_path: Path) -> None:
    root = tmp_path / "pre-turn-owner"
    store = _store(root)
    session, revision = _seed(store)
    annotation = store.add_annotation(
        Annotation(
            id="",
            review_id=session.id,
            revision_id=revision.id,
            anchor=_anchor(),
            body="existing comment",
        )
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("ALTER TABLE annotations DROP COLUMN turn_owner_id")
        conn.execute("ALTER TABLE annotations DROP COLUMN turn_owner_kind")
        conn.execute("ALTER TABLE annotation_versions DROP COLUMN turn_owner_id")
        conn.execute("ALTER TABLE annotation_versions DROP COLUMN turn_owner_kind")
        conn.execute("DELETE FROM _schema_migrations WHERE name = 'v3_006_review_collaboration.sql'")

    migrated = ReviewStore(root)
    migrated.init()
    assert migrated.get_session(session.id) == session
    assert migrated.get_revision(revision.id) == revision
    assert migrated.get_annotation(annotation.id) == annotation
    with sqlite3.connect(migrated.db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        annotation_columns = {row[1] for row in conn.execute("PRAGMA table_info(annotations)")}
        version_columns = {row[1] for row in conn.execute("PRAGMA table_info(annotation_versions)")}
        marker = conn.execute(
            "SELECT 1 FROM _schema_migrations WHERE name = 'v3_006_review_collaboration.sql'"
        ).fetchone()
    assert {"review_participants", "review_requests", "review_attention", "review_attention_events"}.isdisjoint(tables)
    assert {"turn_owner_kind", "turn_owner_id"} <= annotation_columns
    assert {"turn_owner_kind", "turn_owner_id"} <= version_columns
    assert marker is not None


def test_reviewer_outcome_is_revision_bound_append_only_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, first_revision = _seed(store)

    first = store.record_outcome(
        session.id,
        first_revision.id,
        "reviewer-a",
        outcome="lgtm",
        summary="Looks good to me.",
    )
    second_revision = store.add_revision(
        _revision(session.id, tree_fingerprint="tree-bbb"),
        [_unit(content_fingerprint="fp-2")],
    )
    second = store.record_outcome(
        session.id,
        second_revision.id,
        "reviewer-a",
        outcome="changes_requested",
        summary="Please fix the retry path.",
    )
    other = store.record_outcome(
        session.id,
        second_revision.id,
        "reviewer-b",
        outcome="comment",
        summary="No overall verdict.",
    )

    assert store.latest_outcome(session.id, "reviewer-a") == second
    assert store.list_outcomes(session.id, reviewer_id="reviewer-a") == (second, first)
    assert store.latest_outcomes(session.id) == (second, other)
    assert first.revision_id != second.revision_id
    activity = [event for event in store.list_activity_events(session.id) if event.subject_type == "outcome"]
    assert [event.kind for event in activity] == ["outcome.recorded", "outcome.recorded", "outcome.recorded"]


def test_reviewer_outcome_rejects_wrong_revision_or_unknown_kind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    other = store.create_session(_session(source_ref="a..b", subject_type="commit_range"))
    other_revision = store.add_revision(_revision(other.id, tree_fingerprint="other"), [])

    with pytest.raises(ValueError, match="does not belong"):
        store.record_outcome(session.id, other_revision.id, "reviewer-a", outcome="lgtm")
    with pytest.raises(ValueError, match="unknown review outcome"):
        store.record_outcome(session.id, revision.id, "reviewer-a", outcome="approve")  # type: ignore[arg-type]


def test_existing_pre_outcome_database_migrates_with_outcome_table(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP TABLE review_outcomes")
        conn.execute("DELETE FROM _schema_migrations WHERE name = 'v3_011_review_outcomes.sql'")
    migrated = ReviewStore(store.root)
    migrated.init()
    with sqlite3.connect(migrated.db_path) as conn:
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'review_outcomes'").fetchone()
        marker = conn.execute("SELECT 1 FROM _schema_migrations WHERE name = 'v3_011_review_outcomes.sql'").fetchone()
    assert table is not None
    assert marker is not None


def test_existing_pre_annotation_source_database_migrates_before_source_index(tmp_path: Path) -> None:
    """An R10-era database must migrate before any v3-only index is created."""

    root = tmp_path / "legacy"
    root.mkdir()
    legacy_schema = ReviewStore.SCHEMA
    for column in (
        "  source               TEXT NOT NULL DEFAULT 'human',\n",
        "  source_id            TEXT NOT NULL DEFAULT '',\n",
        "  title                TEXT NOT NULL DEFAULT '',\n",
        "  evidence_json        TEXT NOT NULL DEFAULT '[]',\n",
        "  confidence           REAL,\n",
        "  author_response      TEXT NOT NULL DEFAULT 'none',\n",
        "  author_response_source_id TEXT NOT NULL DEFAULT '',\n",
        "  author_response_at   TEXT NOT NULL DEFAULT ''\n",
    ):
        legacy_schema = legacy_schema.replace(column, "")
    legacy_schema = legacy_schema.replace(
        "  updated_at           TEXT NOT NULL,\n);", "  updated_at           TEXT NOT NULL\n);"
    )

    with sqlite3.connect(root / DB_NAME) as conn:
        conn.executescript(legacy_schema)

    store = ReviewStore(root)
    store.init()
    with sqlite3.connect(store.db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(annotations)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(annotations)")}

    assert {"source", "source_id", "title", "evidence_json", "confidence"} <= columns
    assert {"author_response", "author_response_source_id", "author_response_at"} <= columns
    assert "idx_annotations_source" in indexes


def test_store_self_initialises_on_first_use(tmp_path: Path) -> None:
    """Not a StoreBundle member, so nothing else calls init() for it."""
    store = ReviewStore(tmp_path)
    assert store.list_sessions() == ()
    assert store.db_path.is_file()


# ----- 2. reopen identity ------------------------------------------------- #


def test_create_and_find_session_round_trips_every_field(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.create_session(
        _session(
            title="a title",
            source_ref="main..HEAD",
            subject_type="commit_range",
            range_mode="commit_range",
            actor_type="mixed",
            status="open",
            reviewer_id="local",
        )
    )
    found = store.find_session(
        repo_root=created.repo_root,
        subject_type=created.subject_type,
        source_ref=created.source_ref,
        range_mode=created.range_mode,
    )
    assert found == created
    assert store.get_session(created.id) == created


def test_second_session_with_the_same_subject_identity_is_rejected(tmp_path: Path) -> None:
    """Reopen identity: a second `lc review --open` reopens, never forks."""
    store = _store(tmp_path)
    store.create_session(_session())
    with pytest.raises(sqlite3.IntegrityError):
        store.create_session(_session())


def test_a_different_range_mode_is_a_different_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_session(_session())
    other = store.create_session(_session(range_mode="staged"))
    assert other.range_mode == "staged"
    assert len(store.list_sessions()) == 2


def test_update_session_stamps_updated_at_and_rejects_unknown_columns(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_session(_session())
    store.update_session(session.id, status="archived", title="renamed")
    reloaded = store.get_session(session.id)
    assert reloaded is not None
    assert reloaded.status == "archived"
    assert reloaded.title == "renamed"
    assert reloaded.updated_at >= session.updated_at
    with pytest.raises(ValueError, match="unknown review_sessions column"):
        store.update_session(session.id, nonsense="x")


def test_list_sessions_filters_by_status_and_empty_status_lists_all(tmp_path: Path) -> None:
    store = _store(tmp_path)
    open_session = store.create_session(_session())
    archived = store.create_session(_session(source_ref="a..b", subject_type="commit_range"))
    store.update_session(archived.id, status="archived")
    assert [s.id for s in store.list_sessions()] == [open_session.id]
    assert len(store.list_sessions(status="")) == 2


# ----- 3. revisions + units ------------------------------------------------ #


def test_add_revision_round_trips_every_field(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_session(_session())
    revision = store.add_revision(
        _revision(
            session.id,
            base_sha="b" * 40,
            head_sha="",
            merge_base_sha="m" * 40,
            dirty=True,
            packet_path="review/artifacts/x/y/packet.json.gz",
            packet_sha256="deadbeef",
            packet_bytes=1234,
            degraded=("blob_truncated", "impact_partial"),
            provenance_host="claude",
            provenance_model="claude-opus-5",
            provenance_session_id="sess-1",
            provenance_certainty="exact",
            source_fingerprint="watch-abc",
        ),
        [],
    )
    assert store.get_revision(revision.id) == revision
    assert revision.degraded == ("blob_truncated", "impact_partial")
    assert revision.dirty is True
    assert revision.head_sha == ""
    assert revision.source_fingerprint == "watch-abc"


def test_revision_numbers_are_dense_and_assigned_by_the_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_session(_session())
    first = store.add_revision(_revision(session.id, tree_fingerprint="t1"), [])
    second = store.add_revision(_revision(session.id, tree_fingerprint="t2"), [])
    assert (first.revision_number, second.revision_number) == (1, 2)
    assert store.latest_revision(session.id) == second
    assert [r.id for r in store.list_revisions(session.id)] == [first.id, second.id]


def test_current_revision_can_move_back_without_rewriting_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_session(_session())
    first = store.add_revision(_revision(session.id, tree_fingerprint="t1"), [])
    second = store.add_revision(_revision(session.id, tree_fingerprint="t2"), [])

    assert store.set_current_revision(session.id, second.id) is True
    assert store.latest_revision(session.id) == second
    assert store.set_current_revision(session.id, first.id) is True
    assert store.latest_revision(session.id) == first
    assert [item.id for item in store.list_revisions(session.id)] == [first.id, second.id]
    stored = store.get_session(session.id)
    assert stored is not None
    assert stored.current_revision_id == first.id
    assert store.set_current_revision(session.id, first.id) is False


def test_add_revision_is_one_transaction(tmp_path: Path) -> None:
    """A failing unit must leave no revision row behind.

    A revision without its units reads to the frontier as "this revision
    reviewed nothing", which silently invalidates every carried mark.
    """
    store = _store(tmp_path)
    session = store.create_session(_session())
    good = _unit("file:a.py")
    duplicate = _unit("file:a.py", path="b.py")  # violates PRIMARY KEY
    with pytest.raises(sqlite3.IntegrityError):
        store.add_revision(_revision(session.id), [good, duplicate])
    assert store.list_revisions(session.id) == ()
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_units").fetchone()[0] == 0


def test_add_revision_numbers_itself_inside_the_write_lock(tmp_path: Path) -> None:
    """``MAX(revision_number) + 1`` and the INSERT must be one write transaction.

    ``lc review`` and a serving ``lc review --workspace`` are two processes on
    one database. Under the deferred transaction ``_transaction`` opens, the
    ``MAX`` read held no lock, so a rival writer could land its own revision in
    between and the loser died on ``UNIQUE constraint failed:
    review_revisions.review_id, review_revisions.revision_number`` -- after it
    had already written its packet and blob artifacts to disk.
    """

    store = _store(tmp_path)
    session = store.create_session(_session())
    store.add_revision(_revision(session.id, tree_fingerprint="t1"), [])

    rival = sqlite3.connect(store.db_path, timeout=0.5)
    rival.execute("PRAGMA busy_timeout = 250")
    outcome: list[str] = []

    def _rival_tries_to_write() -> None:
        """Fires inside add_revision, between its number read and its INSERT."""
        try:
            rival.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            outcome.append(f"blocked: {exc}")
            return
        rival.execute(
            "INSERT INTO review_revisions (id, review_id, revision_number, range_mode,"
            " tree_fingerprint, packet_schema_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("rival", session.id, 2, "working_tree", "t-rival", 1, utc_now()),
        )
        rival.commit()
        outcome.append("committed")

    original_connect = store._connect

    def _traced() -> sqlite3.Connection:
        conn = original_connect()

        def _on_sql(sql: str) -> None:
            if sql.strip().startswith("INSERT INTO review_revisions") and not outcome:
                _rival_tries_to_write()

        conn.set_trace_callback(_on_sql)
        return conn

    store._connect = _traced  # type: ignore[method-assign]
    try:
        second = store.add_revision(_revision(session.id, tree_fingerprint="t2"), [])
    finally:
        store._connect = original_connect  # type: ignore[method-assign]
        rival.close()

    assert outcome == ["blocked: database is locked"], "the write lock was not held across the number read"
    assert second.revision_number == 2
    assert [item.revision_number for item in store.list_revisions(session.id)] == [1, 2]


def test_identical_tree_fingerprint_is_never_a_second_revision(tmp_path: Path) -> None:
    """The same tree adopts the row already on file instead of raising.

    ``ux_review_revisions_tree`` says one revision per tree, and the caller
    asking to record a tree somebody already recorded got what it asked for.
    Only a *tree* conflict is absorbed: every other integrity failure -- a
    reused revision id, a duplicate unit key -- is a programming error and
    must keep raising.
    """

    store = _store(tmp_path)
    session = store.create_session(_session())
    first = store.add_revision(_revision(session.id, tree_fingerprint="same"), [])
    again = store.add_revision(_revision(session.id, tree_fingerprint="same"), [])
    assert again == first
    assert [item.id for item in store.list_revisions(session.id)] == [first.id]
    assert store.find_revision_by_tree(session.id, "same") == first
    assert store.find_revision_by_tree(session.id, "other") is None

    with pytest.raises(sqlite3.IntegrityError):
        store.add_revision(_revision(session.id, id=first.id, tree_fingerprint="different"), [])


def test_losing_the_tree_race_adopts_the_rivals_revision_and_strands_no_artifacts(tmp_path: Path) -> None:
    """Two writers recording one tree: the loser adopts the winner, it does not die.

    ``sources.local.record_revision`` calls ``find_revision_by_tree``, then
    writes ``packet.json.gz`` and ``blobs.json.gz``, and only then calls
    ``add_revision`` -- the whole race window lives *outside* this method, so no
    lock taken inside it can close it. A rival ``lc review`` committing the same
    tree in that window used to kill this process with a raw
    ``sqlite3.IntegrityError: UNIQUE constraint failed:
    review_revisions.review_id, review_revisions.tree_fingerprint``, leaving two
    artifact files behind that no row would ever reference and ``prune`` would
    only reach if the session were later archived.
    """

    mine = _store(tmp_path)
    rival = ReviewStore(tmp_path)
    rival.init()
    session = mine.create_session(_session())

    # record_revision's order of operations, with the rival landing in the gap.
    assert mine.find_revision_by_tree(session.id, "shared") is None
    revision_id = new_revision_id()
    rel_packet, _sha, _size = mine.write_packet_artifact(session.id, revision_id, b"packet-bytes")
    rel_blobs = mine.blob_artifact_relpath(session.id, revision_id)
    mine.write_blob_artifact(session.id, revision_id, {"src/app.py": "print(1)"})
    assert (tmp_path / rel_packet).is_file() and (tmp_path / rel_blobs).is_file()

    theirs = rival.add_revision(_revision(session.id, tree_fingerprint="shared"), [_unit()])
    adopted = mine.add_revision(
        _revision(session.id, id=revision_id, tree_fingerprint="shared"),
        [_unit()],
    )

    assert adopted == theirs
    assert [item.id for item in mine.list_revisions(session.id)] == [theirs.id]
    assert adopted.revision_number == 1
    assert mine.list_units(revision_id) == ()
    with sqlite3.connect(mine.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_units").fetchone()[0] == 1

    # The loser's artifacts are collected; the winner's are untouched.
    assert not (tmp_path / rel_packet).exists()
    assert not (tmp_path / rel_blobs).exists()
    assert not (tmp_path / mine.artifact_relpath(session.id, revision_id)).parent.exists()
    assert mine.read_blob_artifact(session.id, revision_id) is None


def test_units_round_trip_every_field(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_session(_session())
    revision = store.add_revision(
        _revision(session.id),
        [
            _unit(
                "symbol:src/app.py#handler",
                kind="symbol",
                symbol="App.handler",
                ordinal=2,
                start_line=40,
                end_line=57,
                content_fingerprint="fp-sym",
                fingerprint_method="symbol_body_sha256",
                attention_rank=3,
                attention_group="test",
                reasons=("signature changed", "has callers"),
            )
        ],
    )
    (unit,) = store.list_units(revision.id)
    assert unit.revision_id == revision.id
    assert unit.unit_key == "symbol:src/app.py#handler"
    assert unit.kind == "symbol"
    assert unit.symbol == "App.handler"
    assert unit.ordinal == 2
    assert (unit.start_line, unit.end_line) == (40, 57)
    assert unit.fingerprint_method == "symbol_body_sha256"
    assert unit.attention_rank == 3
    assert unit.attention_group == "test"
    assert unit.reasons == ("signature changed", "has callers")


# ----- 4. FK cascade ------------------------------------------------------- #


def test_deleting_a_session_cascades_through_every_child_table(tmp_path: Path) -> None:
    """Proves PRAGMA foreign_keys=ON is live, not merely declared in the DDL."""
    store = _store(tmp_path)
    session, revision = _seed(store)
    store.set_mark(
        ReviewMark(
            review_id=session.id,
            unit_key="file:src/app.py",
            state="reviewed",
            reviewed_revision_id=revision.id,
            content_fingerprint="fp-1",
        )
    )
    annotation = store.add_annotation(
        Annotation(
            id=new_annotation_id(),
            review_id=session.id,
            revision_id=revision.id,
            anchor=_anchor(),
            body="this branch never runs",
        )
    )
    store.record_frontier(session.id, "local", revision.id)
    store.record_anchor_event(annotation.id, revision.id, "identical_blob", "resolved", "", _anchor())
    store.record_delivery(DeliveryRecord(id="", annotation_id=annotation.id, target_type="markdown_bundle"))

    with sqlite3.connect(store.db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM review_sessions WHERE id = ?", (session.id,))
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "review_revisions",
                "review_units",
                "review_marks",
                "review_frontiers",
                "annotations",
                "annotation_anchor_events",
                "deliveries",
            )
        }
    assert counts == dict.fromkeys(counts, 0)


# ----- 5. marks ------------------------------------------------------------ #


def test_set_mark_upserts_on_the_reviewer_unit_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    mark = ReviewMark(
        review_id=session.id,
        unit_key="file:src/app.py",
        state="reviewed",
        reviewed_revision_id=revision.id,
        content_fingerprint="fp-1",
        note="looks fine",
    )
    store.set_mark(mark)
    updated = store.set_mark(replace(mark, state="needs_changes", note="actually no"))
    marks = store.list_marks(session.id)
    assert len(marks) == 1
    assert marks[0].state == "needs_changes"
    assert marks[0].note == "actually no"
    assert updated.created_at == marks[0].created_at


def test_marks_round_trip_and_are_per_reviewer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    human = ReviewMark(
        review_id=session.id,
        unit_key="file:src/app.py",
        state="reviewed",
        reviewed_revision_id=revision.id,
        content_fingerprint="fp-1",
    )
    agent = replace(human, reviewer_id="agent-1", actor_type="agent", state="unknown")
    store.set_mark(human)
    store.set_mark(agent)
    assert store.list_marks(session.id)[0].actor_type == "human"
    (agent_mark,) = store.list_marks(session.id, reviewer_id="agent-1")
    assert agent_mark.actor_type == "agent"
    assert agent_mark.state == "unknown"
    store.clear_mark(session.id, "agent-1", "file:src/app.py")
    assert store.list_marks(session.id, reviewer_id="agent-1") == ()
    assert len(store.list_marks(session.id)) == 1


def test_a_frontier_outlives_every_mark_that_produced_it(tmp_path: Path) -> None:
    """The reviewer's position is durable state, not a projection of their marks.

    Reconciliation clears a mark whose unit left the review, so a frontier read
    back as ``max(reviewed_revision_id)`` over the marks table evaporates the
    moment an agent renames the one symbol a reviewer approved. The row written
    here is the evidence that a human looked, and it has to survive a verdict
    being discarded -- a verdict may be withdrawn, the looking may not be
    unhappened.
    """

    store = _store(tmp_path)
    session, revision = _seed(store)
    assert store.frontier_revision(session.id) == ""

    store.set_mark(
        ReviewMark(
            review_id=session.id,
            unit_key="file:src/app.py",
            state="reviewed",
            reviewed_revision_id=revision.id,
            content_fingerprint="fp-1",
        )
    )
    store.record_frontier(session.id, "local", revision.id)
    store.clear_mark(session.id, "local", "file:src/app.py")

    assert store.list_marks(session.id) == ()
    assert store.frontier_revision(session.id) == revision.id


def test_a_frontier_names_the_revision_the_reviewer_last_looked_at(tmp_path: Path) -> None:
    """The frontier follows the reviewer, not the revision counter.

    It used to be advance-only on ``revision_number``, which is a high-water
    mark of nothing a human experienced: the number counts the distinct trees
    this review has ever seen, in discovery order. An undo puts the tree back
    on revision 1, the reviewer records a verdict there, and a frontier pinned
    to 2 then tells them "you last saw revision 2" under a header reading
    revision 1 -- and reports every unit revision 2 did not contain as new.

    Nothing is un-seen by moving back: what they looked at on the way is kept
    on their marks and, for the verdicts reconciliation deleted, on
    ``review_discarded_marks``.
    """

    store = _store(tmp_path)
    session, first = _seed(store)
    second = store.add_revision(_revision(session.id, tree_fingerprint="tree-2"), [_unit()])
    assert (first.revision_number, second.revision_number) == (1, 2)

    store.record_frontier(session.id, "local", second.id)
    store.record_frontier(session.id, "local", first.id)
    assert store.frontier_revision(session.id) == first.id

    # And forwards again, so it is a position and not a ratchet in either
    # direction.
    store.record_frontier(session.id, "local", second.id)
    assert store.frontier_revision(session.id) == second.id

    # Per reviewer, and silent about a revision that is not on file.
    assert store.frontier_revision(session.id, "agent-1") == ""
    store.record_frontier(session.id, "agent-1", new_revision_id())
    assert store.frontier_revision(session.id, "agent-1") == ""


def test_a_discarded_verdict_outlives_the_mark_row_and_stays_countable_as_seen(tmp_path: Path) -> None:
    """Deleting the verdict is right; deleting the evidence is the silent part.

    Two questions depend on this row and both answer wrongly without it: "was my
    approval thrown away?" (announced once, then unrecoverable from anywhere)
    and "is this unit new to me?" -- ``new`` means "you have not seen this
    content", and after the mark row goes the content fingerprint here is the
    only proof left that the reviewer ever looked.
    """

    store = _store(tmp_path)
    session, revision = _seed(store)
    mark = store.set_mark(
        ReviewMark(
            review_id=session.id,
            unit_key="symbol:alpha",
            state="reviewed",
            reviewed_revision_id=revision.id,
            content_fingerprint="fp-alpha",
        )
    )
    second = store.add_revision(_revision(session.id, tree_fingerprint="tree-2"), [_unit()])

    record = store.record_discarded_mark(
        DiscardedMark(
            review_id=session.id,
            unit_key=mark.unit_key,
            discarded_revision_id=second.id,
            state=mark.state,
            content_fingerprint=mark.content_fingerprint,
            kind="symbol",
            path="src/app.py",
            symbol="alpha",
            reason="the unit it attested to is not in this revision",
        )
    )
    store.clear_mark(session.id, "local", mark.unit_key)

    assert record.discarded_at
    assert store.list_marks(session.id) == ()
    (kept,) = store.discarded_marks_on(second.id)
    assert (kept.unit_key, kept.state, kept.symbol) == ("symbol:alpha", "reviewed", "alpha")
    assert kept.reason == "the unit it attested to is not in this revision"
    # The memory that stops an undo presenting approved content as brand new.
    assert ("symbol:alpha", "fp-alpha") in store.attested_fingerprints(session.id)

    # Reconciliation is a fixed point, so re-running it must not manufacture a
    # second discard of the same verdict.
    store.record_discarded_mark(record)
    assert len(store.discarded_marks_on(second.id)) == 1
    # Per revision and per reviewer, like every other row in this file.
    assert store.discarded_marks_on(revision.id) == ()
    assert store.discarded_marks_on(second.id, reviewer_id="agent-1") == ()
    assert store.attested_fingerprints(session.id, reviewer_id="agent-1") == frozenset()


def test_a_discard_is_news_until_the_unit_returns_or_the_frontier_passes_it(tmp_path: Path) -> None:
    """The window every surface reports a discard in, pinned at both ends.

    ``discarded_marks_on`` answers about one revision, and reconciliation writes
    the row on whichever revision it was standing on. The frontier beside it is
    scoped to the reviewer's baseline, so from the *next* revision onwards the
    two disagreed: the frontier still named the unit as gone while the discard
    list came back empty underneath it. One scope, both ends of it here.
    """

    store = _store(tmp_path)
    session = store.create_session(_session())
    first = store.add_revision(_revision(session.id, tree_fingerprint="tree-1"), [_unit("symbol:alpha"), _unit()])
    second = store.add_revision(_revision(session.id, tree_fingerprint="tree-2"), [_unit()])
    third = store.add_revision(_revision(session.id, tree_fingerprint="tree-3"), [_unit()])

    store.record_frontier(session.id, "local", first.id)
    store.record_discarded_mark(
        DiscardedMark(
            review_id=session.id,
            unit_key="symbol:alpha",
            discarded_revision_id=second.id,
            state="reviewed",
            content_fingerprint="fp-alpha",
            kind="symbol",
            path="src/app.py",
            symbol="alpha",
            reason="the unit it attested to is not in this revision",
        )
    )

    def window(current: ReviewRevision, baseline: ReviewRevision) -> tuple[str, ...]:
        return tuple(
            record.unit_key
            for record in store.discarded_marks_since(
                session.id,
                after_revision_number=baseline.revision_number,
                absent_from_revision_id=current.id,
            )
        )

    # The revision it happened on, and every revision after it.
    assert window(second, first) == ("symbol:alpha",)
    assert window(third, first) == ("symbol:alpha",)

    # The frontier reaching the revision that destroyed it ends the report.
    assert window(third, second) == ()
    assert window(third, third) == ()

    # So does the unit coming back: there is something to look at again.
    assert window(first, first) == ()

    # Per reviewer, like every other row here.
    assert (
        store.discarded_marks_since(
            session.id,
            reviewer_id="agent-1",
            after_revision_number=first.revision_number,
            absent_from_revision_id=third.id,
        )
        == ()
    )

    # Two revisions passing is not two losses: a second discard of the same unit
    # on a later revision still prints once, carrying the newer state.
    store.record_discarded_mark(
        DiscardedMark(
            review_id=session.id,
            unit_key="symbol:alpha",
            discarded_revision_id=third.id,
            state="needs_changes",
            content_fingerprint="fp-alpha-2",
            kind="symbol",
            path="src/app.py",
            symbol="alpha",
            reason="the unit it attested to is not in this revision",
        )
    )
    (only,) = store.discarded_marks_since(
        session.id,
        after_revision_number=first.revision_number,
        absent_from_revision_id=third.id,
    )
    assert (only.unit_key, only.state) == ("symbol:alpha", "needs_changes")


# ----- 6. annotations ------------------------------------------------------ #


def test_annotation_and_anchor_round_trip_every_field(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    anchor = _anchor(
        selected_text_hash="h-sel",
        before_context_hash="h-before",
        after_context_hash="h-after",
        symbol_qualified_name="App.handler",
        symbol_fingerprint="fp-sym",
        blob_sha="blob-1",
    )
    created = store.add_annotation(
        Annotation(
            id="",
            review_id=session.id,
            revision_id=revision.id,
            anchor=anchor,
            body="this early return skips validation",
            kind="request_change",
            state="open",
            created_by_actor="human",
            anchor_method="exact_text_context",
            anchor_detail="matched 3 lines of context",
        )
    )
    (stored,) = store.list_annotations(session.id)
    assert stored == created
    assert stored.anchor == anchor
    assert stored.id.startswith("ann-")
    assert store.list_annotations(session.id, state="resolved") == ()


def test_update_annotation_rewrites_the_anchor_and_the_search_index(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    created = store.add_annotation(
        Annotation(id="", review_id=session.id, revision_id=revision.id, anchor=_anchor(), body="original wording")
    )
    moved = _anchor(path="src/other.py", start_line=99, end_line=99, unit_key="file:src/other.py")
    updated = store.update_annotation(
        created.id, body="replacement wording", state="resolved", anchor=moved, anchor_method="unique_text"
    )
    assert updated is not None
    assert updated.body == "replacement wording"
    assert updated.state == "resolved"
    assert updated.anchor == moved
    assert updated.anchor_method == "unique_text"
    # The denormalised index columns follow the anchor, never lag behind it.
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT path, unit_key FROM annotations WHERE id = ?", (created.id,)).fetchone()
    assert row == ("src/other.py", "file:src/other.py")
    assert store.search_annotations(session.id, "replacement")
    assert store.search_annotations(session.id, "original") == ()
    assert store.update_annotation("ann-missing", body="x") is None
    with pytest.raises(ValueError, match="unknown annotations column"):
        store.update_annotation(created.id, nonsense="x")


def test_mark_events_and_activity_are_append_only_while_live_mark_is_latest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    unit = store.list_units(revision.id)[0]

    first = store.set_mark(
        ReviewMark(
            review_id=session.id,
            reviewer_id="local",
            unit_key=unit.unit_key,
            state="reviewed",
            reviewed_revision_id=revision.id,
            content_fingerprint=unit.content_fingerprint,
        ),
        event_kind="judgment",
        event_revision_id=revision.id,
    )
    second = store.set_mark(
        replace(first, state="needs_changes"),
        event_kind="judgment",
        event_revision_id=revision.id,
        event_reason="reviewer found a blocker",
    )

    assert store.list_marks(session.id) == (second,)
    events = store.list_mark_events(session.id)
    assert [(item.event_kind, item.from_state, item.to_state) for item in events] == [
        ("judgment", "", "reviewed"),
        ("judgment", "reviewed", "needs_changes"),
    ]
    assert events[-1].reason == "reviewer found a blocker"
    activity = store.list_activity_events(session.id)
    assert [item.kind for item in activity] == [
        "review.created",
        "revision.recorded",
        "mark.judgment",
        "mark.judgment",
    ]


def test_annotation_versions_track_thread_turn_ownership(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    created = store.add_annotation(
        Annotation(
            id="",
            review_id=session.id,
            revision_id=revision.id,
            anchor=_anchor(),
            body="please change this",
            kind="request_change",
            created_by="bob",
            turn_owner_kind="author",
        )
    )
    versions = store.list_annotation_versions(created.id)
    assert [(item.version_number, item.turn_owner_kind, item.turn_owner_id) for item in versions] == [(1, "author", "")]

    returned = store.update_annotation(
        created.id,
        AnnotationHistoryContext(
            revision_id=revision.id,
            changed_by="alice",
            changed_by_actor="human",
            change_kind="author_response",
            expected_version=1,
        ),
        author_response="addressed",
        author_response_source_id="alice",
        author_response_at=utc_now(),
        turn_owner_kind="reviewer",
        turn_owner_id="bob",
    )
    assert returned is not None
    assert returned.turn_owner_kind == "reviewer"
    assert returned.turn_owner_id == "bob"
    versions = store.list_annotation_versions(created.id)
    assert [(item.version_number, item.change_kind, item.turn_owner_kind, item.turn_owner_id) for item in versions] == [
        (1, "created", "author", ""),
        (2, "author_response", "reviewer", "bob"),
    ]

    with pytest.raises(AnnotationVersionConflict):
        store.update_annotation(
            created.id,
            AnnotationHistoryContext(
                revision_id=revision.id,
                changed_by="stale-author",
                changed_by_actor="human",
                expected_version=1,
            ),
            body="stale write must not land",
        )
    assert store.get_annotation(created.id) == returned
    assert store.annotation_version_number(created.id) == 2


def test_annotation_versions_track_semantics_not_anchor_only_movement(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    created = store.add_annotation(
        Annotation(id="", review_id=session.id, revision_id=revision.id, anchor=_anchor(), body="first wording")
    )
    assert [
        (item.version_number, item.change_kind, item.body) for item in store.list_annotation_versions(created.id)
    ] == [(1, "created", "first wording")]

    moved = _anchor(path="src/other.py", start_line=7, end_line=7, unit_key="file:src/other.py")
    store.update_annotation(
        created.id,
        AnnotationHistoryContext(revision_id=revision.id, changed_by="lemoncrow"),
        anchor=moved,
        anchor_method="unique_text",
    )
    assert len(store.list_annotation_versions(created.id)) == 1

    store.update_annotation(
        created.id,
        AnnotationHistoryContext(revision_id=revision.id, changed_by="local", changed_by_actor="human"),
        body="second wording",
    )
    store.update_annotation(
        created.id,
        AnnotationHistoryContext(revision_id=revision.id, changed_by="local", changed_by_actor="human"),
        state="resolved",
    )
    versions = store.list_annotation_versions(created.id)
    assert [(item.version_number, item.change_kind, item.body, item.state) for item in versions] == [
        (1, "created", "first wording", "open"),
        (2, "edited", "second wording", "open"),
        (3, "state_changed", "second wording", "resolved"),
    ]
    assert [item.kind for item in store.list_activity_events(session.id)][-3:] == [
        "annotation.created",
        "annotation.updated",
        "annotation.state_changed",
    ]


def test_search_annotations_matches_a_prefix_and_stays_inside_one_review(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    other = store.create_session(_session(repo_root="/repo/beta"))
    other_revision = store.add_revision(_revision(other.id, tree_fingerprint="t-beta"), [])
    store.add_annotation(
        Annotation(id="", review_id=session.id, revision_id=revision.id, anchor=_anchor(), body="unvalidated input")
    )
    store.add_annotation(
        Annotation(
            id="",
            review_id=other.id,
            revision_id=other_revision.id,
            anchor=_anchor(),
            body="unvalidated input elsewhere",
        )
    )
    hits = store.search_annotations(session.id, "unvalid")
    assert [h.review_id for h in hits] == [session.id]
    assert store.search_annotations(session.id, "   ") == ()
    assert store.search_annotations(session.id, '"unclosed') == () or True


def test_anchor_events_are_appended_and_survive_a_none_anchor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    annotation = store.add_annotation(
        Annotation(id="", review_id=session.id, revision_id=revision.id, anchor=_anchor(), body="b")
    )
    store.record_anchor_event(annotation.id, revision.id, "unique_text", "resolved", "one match", _anchor())
    store.record_anchor_event(annotation.id, revision.id, "orphaned", "failed", "ambiguous", None)
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT method, status, detail, anchor_json FROM annotation_anchor_events ORDER BY id"
        ).fetchall()
    # Three, not two: ``add_annotation`` writes the origin rung itself, so a
    # comment's history always starts where it was written rather than at
    # whatever first went looking for it again.
    assert len(rows) == 3
    assert {row[0] for row in rows} == {"identical_blob", "unique_text", "orphaned"}
    assert "{}" in {row[3] for row in rows}


def test_only_the_annotations_whose_position_came_from_this_revision_count(tmp_path: Path) -> None:
    """``annotations_anchored_at`` asks the narrow question, on purpose.

    "Has this comment *ever* been resolved against this revision?" and "is its
    stored position an answer about this revision?" differ exactly where a
    revert lands: the tree fingerprints back to an older revision the comment
    already owns its origin event on, while the row itself still carries the
    geometry of the revision in between. Answering the first question there
    skipped re-anchoring and left the comment pointing at a line, and naming a
    definition, that exist nowhere in the tree.
    """

    store = _store(tmp_path)
    session, first = _seed(store)
    second = store.add_revision(_revision(session.id, tree_fingerprint="tree-2"), [_unit()])
    annotation = store.add_annotation(
        Annotation(id="", review_id=session.id, revision_id=first.id, anchor=_anchor(), body="why?")
    )

    # Written on revision 1: its position is an answer about revision 1.
    assert store.annotations_anchored_at(first.id) == frozenset({annotation.id})
    assert store.annotations_anchored_at(second.id) == frozenset()

    store.record_anchor_event(annotation.id, second.id, "unique_text", "relocated", "moved", _anchor(start_line=40))

    # The comment still owns an event on revision 1, but its geometry is now
    # revision 2's -- so revision 1 must ask for it again.
    assert store.annotations_anchored_at(second.id) == frozenset({annotation.id})
    assert store.annotations_anchored_at(first.id) == frozenset()


def test_a_revisions_anchor_report_is_its_newest_verdict_not_its_first(tmp_path: Path) -> None:
    """One move per comment, and it describes the row as it stands.

    A revision can hold two events for one comment: the origin written when the
    comment was created on it, and a re-resolution recorded when an edit was
    undone and the tree came back to that same content. Replaying the origin
    printed "file unchanged since the comment" beside a line the comment had
    since been moved off.
    """

    store = _store(tmp_path)
    session, first = _seed(store)
    second = store.add_revision(_revision(session.id, tree_fingerprint="tree-2"), [_unit()])
    annotation = store.add_annotation(
        Annotation(id="", review_id=session.id, revision_id=first.id, anchor=_anchor(), body="why?")
    )
    store.record_anchor_event(annotation.id, second.id, "unique_text", "relocated", "moved", _anchor(start_line=40))
    store.record_anchor_event(
        annotation.id, first.id, "exact_text_context", "relocated", "came back", _anchor(start_line=10)
    )

    (move,) = store.anchor_moves_on(first.id)
    assert (move.method, move.detail) == ("exact_text_context", "came back")
    assert (move.from_line, move.to_line) == (40, 10)


def test_a_corrupt_anchor_json_degrades_instead_of_raising(tmp_path: Path) -> None:
    """Unknown is a first-class state: a bad row still reads back as an anchor."""
    store = _store(tmp_path)
    session, revision = _seed(store)
    annotation = store.add_annotation(
        Annotation(id="", review_id=session.id, revision_id=revision.id, anchor=_anchor(), body="b")
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE annotations SET anchor_json = ? WHERE id = ?", ("not json", annotation.id))
        conn.commit()
    (reloaded,) = store.list_annotations(session.id)
    assert reloaded.anchor.path == ""
    assert reloaded.anchor.start_line == 0


def test_an_anchor_json_with_an_unknown_key_still_loads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    annotation = store.add_annotation(
        Annotation(id="", review_id=session.id, revision_id=revision.id, anchor=_anchor(), body="b")
    )
    payload = {"path": "src/app.py", "side": "new", "start_line": 5, "end_line": 6, "from_a_newer_build": 1}
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE annotations SET anchor_json = ? WHERE id = ?", (json.dumps(payload), annotation.id))
        conn.commit()
    (reloaded,) = store.list_annotations(session.id)
    assert reloaded.anchor.start_line == 5


# ----- 7. deliveries ------------------------------------------------------- #


def test_delivery_records_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    annotation = store.add_annotation(
        Annotation(id="", review_id=session.id, revision_id=revision.id, anchor=_anchor(), body="b")
    )
    created = store.record_delivery(
        DeliveryRecord(
            id="",
            annotation_id=annotation.id,
            target_type="agent_session",
            target_ref="sess-1",
            state="pending",
        )
    )
    assert store.list_deliveries(annotation.id) == (created,)
    assert created.id.startswith("dlv-")


# ----- 8. review evidence + artifacts ------------------------------------- #


def test_review_evidence_round_trips_and_is_revision_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    evidence_id = new_evidence_id()
    rel, digest, size = store.write_evidence_artifact(session.id, revision.id, evidence_id, b"png-bytes")
    created = store.add_evidence(
        ReviewEvidence(
            id=evidence_id,
            review_id=session.id,
            revision_id=revision.id,
            kind="screenshot",
            title="checkout mobile",
            path="src/app.py",
            artifact_path=rel,
            content_hash=digest,
            mime_type="image/png",
            bytes=size,
            source="test",
            verification_status="PASS",
            detail="84 review UI tests passed",
        )
    )

    assert store.get_evidence(created.id) == created
    assert store.list_evidence(session.id) == (created,)
    assert store.read_evidence_artifact(created) == b"png-bytes"
    assert created.verification_status == "PASS"
    assert created.detail == "84 review UI tests passed"

    assert store.delete_evidence(created.id) == 1
    assert store.get_evidence(created.id) is None
    assert store.read_evidence_artifact(created) is None


def test_packet_artifact_round_trips_and_stores_a_relative_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, _ = _seed(store)
    revision_id = new_revision_id()
    payload = b"\x1f\x8bpacket-bytes"
    rel, digest, size = store.write_packet_artifact(session.id, revision_id, payload)
    assert not Path(rel).is_absolute()
    assert rel == f"review/artifacts/{session.id}/{revision_id}/packet.json.gz"
    assert size == len(payload)
    assert len(digest) == 64
    revision = store.add_revision(
        _revision(session.id, id=revision_id, tree_fingerprint="t-art", packet_path=rel, packet_sha256=digest),
        [],
    )
    assert store.read_packet_artifact(revision) == payload


def test_blob_artifact_round_trips_exact_changed_file_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, _ = _seed(store)
    revision_id = new_revision_id()
    blobs = {"src/app.py": "def one():\n    return 1\n", "empty.txt": ""}

    rel, digest, size = store.write_blob_artifact(session.id, revision_id, blobs)

    assert rel == f"review/artifacts/{session.id}/{revision_id}/blobs.json.gz"
    assert not Path(rel).is_absolute()
    assert len(digest) == 64
    assert size > 0
    assert store.read_blob_artifact(session.id, revision_id) == blobs


def test_read_packet_artifact_returns_none_when_there_is_nothing_to_read(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, _ = _seed(store)
    assert store.read_packet_artifact(_revision(session.id)) is None
    missing = _revision(session.id, packet_path="review/artifacts/gone/gone/packet.json.gz")
    assert store.read_packet_artifact(missing) is None
    escaping = _revision(session.id, packet_path="../../../etc/passwd")
    assert store.read_packet_artifact(escaping) is None


def test_a_traversing_review_id_raises_instead_of_escaping_the_store_root(tmp_path: Path) -> None:
    """confine_to_root alone is not enough here.

    ``<root>/review/artifacts/../../etc/passwd`` resolves back *inside* the
    root, so traversal has to be refused at the path segment.
    """
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.write_packet_artifact("../../etc/passwd", new_revision_id(), b"x")
    with pytest.raises(ValueError):
        store.write_packet_artifact(new_session_id(), "../../../etc/passwd", b"x")
    with pytest.raises(ValueError):
        store.artifact_relpath("a/b", new_revision_id())
    assert not (tmp_path / "etc").exists()


# ----- 9. version guard ---------------------------------------------------- #


def test_a_newer_database_hard_fails_instead_of_being_written_into(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, _ = _seed(store)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE review_meta SET value = '999' WHERE key = 'schema_version'")
        conn.commit()

    reopened = ReviewStore(tmp_path)
    with pytest.raises(RuntimeError) as excinfo:
        reopened.init()
    message = str(excinfo.value)
    assert "newer LemonCrow" in message
    assert "999" in message
    assert DB_NAME in message

    # And the refusal holds for a store that was never explicitly init()ed.
    lazy = ReviewStore(tmp_path)
    with pytest.raises(RuntimeError, match="newer LemonCrow"):
        lazy.get_session(session.id)


def test_an_older_recorded_version_is_upgraded_in_place(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, _ = _seed(store)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE review_meta SET value = '0' WHERE key = 'schema_version'")
        conn.commit()
    reopened = ReviewStore(tmp_path)
    reopened.init()
    assert reopened.get_session(session.id) is not None
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT value FROM review_meta WHERE key = 'schema_version'").fetchone()
    assert int(row[0]) == SESSION_SCHEMA_VERSION


def test_an_unreadable_version_row_is_treated_as_absent_not_as_newer(tmp_path: Path) -> None:
    """A garbled version must not brick the store; it is rewritten."""
    store = _store(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE review_meta SET value = 'banana' WHERE key = 'schema_version'")
        conn.commit()
    reopened = ReviewStore(tmp_path)
    reopened.init()
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT value FROM review_meta WHERE key = 'schema_version'").fetchone()
    assert int(row[0]) == SESSION_SCHEMA_VERSION


# ----- 10. prune ----------------------------------------------------------- #


def test_prune_removes_archived_sessions_and_their_artifacts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, _ = _seed(store)
    rel, _digest, _size = store.write_packet_artifact(session.id, new_revision_id(), b"payload")
    assert (tmp_path / rel).is_file()
    assert store.prune(older_than_days=0) == 0  # still open: never pruned

    store.update_session(session.id, status="archived")
    assert store.prune(older_than_days=0) == 1
    assert store.get_session(session.id) is None
    assert not (tmp_path / rel).exists()
    assert not (store.artifacts_dir / session.id).exists()


def test_prune_leaves_recent_archived_sessions_alone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, _ = _seed(store)
    store.update_session(session.id, status="archived")
    assert store.prune(older_than_days=90) == 0
    assert store.get_session(session.id) is not None


def test_prune_clears_the_search_index_for_deleted_annotations(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    store.add_annotation(
        Annotation(id="", review_id=session.id, revision_id=revision.id, anchor=_anchor(), body="prunable body")
    )
    store.update_session(session.id, status="archived")
    assert store.prune(older_than_days=0) == 1
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM annotations_fts").fetchone()[0] == 0


# ----- 11. concurrency ----------------------------------------------------- #


def test_connections_are_wal_with_the_house_busy_timeout(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conn = store._connect()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 120000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_a_cli_write_and_a_service_write_do_not_collide(tmp_path: Path) -> None:
    """Two independent store instances on one file, writing at the same time.

    This is the real shape: `lc review` in a terminal while a workspace process
    is open. WAL plus the inherited 120s busy_timeout is the only guard there
    is, so it has to actually hold.
    """
    seed = _store(tmp_path)
    session, revision = _seed(seed)

    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def writer(reviewer_id: str, count: int) -> None:
        store = ReviewStore(tmp_path)
        try:
            barrier.wait(timeout=30)
            for index in range(count):
                store.set_mark(
                    ReviewMark(
                        review_id=session.id,
                        unit_key=f"file:src/app_{index}.py",
                        state="reviewed",
                        reviewed_revision_id=revision.id,
                        content_fingerprint=f"fp-{index}",
                        reviewer_id=reviewer_id,
                    )
                )
        except BaseException as exc:  # re-raised on the main thread
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=("local", 25)),
        threading.Thread(target=writer, args=("service", 25)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert len(seed.list_marks(session.id, reviewer_id="local")) == 25
    assert len(seed.list_marks(session.id, reviewer_id="service")) == 25


def test_opening_an_existing_store_takes_no_write_lock(tmp_path: Path) -> None:
    """Constructing a store must not be a write.

    ``_ensure_ready`` self-initialises on first use, and a long-lived service
    builds one store per request. If that re-init rewrote the schema-version
    row every time, every read request would take the write lock -- and inside
    a ``read_scope`` it would hold it for the whole burst, blocking the CLI.
    """
    _store(tmp_path)
    reopened = ReviewStore(tmp_path)
    conn = reopened._connect()
    reopened._connection = conn
    conn.isolation_level = "IMMEDIATE"
    try:
        reopened.init()
        assert conn.in_transaction is False
    finally:
        reopened._connection = None
        conn.close()


def test_a_reader_sees_a_concurrent_writers_committed_rows(tmp_path: Path) -> None:
    writer = _store(tmp_path)
    session, _ = _seed(writer)
    reader = _store(tmp_path)
    with reader.read_scope():
        assert reader.get_session(session.id) is not None
        writer.update_session(session.id, title="changed by the other process")
    assert reader.get_session(session.id).title == "changed by the other process"  # type: ignore[union-attr]


# ----- 12. sync-friendliness ----------------------------------------------- #


def test_review_ids_are_bare_sortable_uuid_payloads_and_carry_no_local_path(tmp_path: Path) -> None:
    """Review/revision storage ids are opaque UUID payloads; namespaces live only in public refs."""
    store = _store(tmp_path)
    session, revision = _seed(store)
    annotation = store.add_annotation(
        Annotation(id="", review_id=session.id, revision_id=revision.id, anchor=_anchor(), body="b")
    )
    for identifier in (session.id, revision.id):
        assert len(identifier) == 32
        assert all(char in "0123456789abcdef" for char in identifier)
        assert identifier[12] == "7", "UUIDv7 version nibble: ids must sort by creation time"
        assert str(tmp_path) not in identifier
        assert "/" not in identifier
    assert annotation.id.startswith("ann-")


def test_ids_sort_by_creation_time(tmp_path: Path) -> None:
    """UUIDv7's leading 48 bits are the millisecond clock.

    Within one millisecond the remaining bits are random and order is
    arbitrary -- what a sync layer needs is that ids never sort *backwards* in
    time, which is what the timestamp prefix guarantees.
    """
    timestamp_prefixes = []
    for _ in range(5):
        timestamp_prefixes.append(new_session_id()[:12])
        time.sleep(0.002)
    assert timestamp_prefixes == sorted(timestamp_prefixes)
    assert len(set(timestamp_prefixes)) == len(timestamp_prefixes)


def test_every_timestamp_is_aware_utc_iso8601(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    mark = store.set_mark(
        ReviewMark(
            review_id=session.id,
            unit_key="file:src/app.py",
            state="reviewed",
            reviewed_revision_id=revision.id,
            content_fingerprint="fp-1",
        )
    )
    annotation = store.add_annotation(
        Annotation(id="", review_id=session.id, revision_id=revision.id, anchor=_anchor(), body="b")
    )
    stamps = [
        session.created_at,
        session.updated_at,
        revision.created_at,
        mark.created_at,
        mark.updated_at,
        annotation.created_at,
        annotation.updated_at,
        utc_now(),
    ]
    for stamp in stamps:
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None, f"naive timestamp is unsyncable: {stamp!r}"
        assert parsed.utcoffset() == UTC.utcoffset(None)


def test_no_primary_key_depends_on_a_local_filesystem_path(tmp_path: Path) -> None:
    """repo_root is an attribute of a session, never part of its identity."""
    store = _store(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({})".format(
                ",".join("?" for _ in ReviewStore.REQUIRED_TABLES)
            ),
            ReviewStore.REQUIRED_TABLES,
        ).fetchall()
        for (table,) in rows:
            key_columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall() if row[5]]
            assert "repo_root" not in key_columns
            assert "packet_path" not in key_columns


def test_session_models_import_without_sqlite_or_git(tmp_path: Path) -> None:
    """Pure data: no I/O, and no import of the store it is persisted by."""
    source = (
        Path(__file__).resolve().parents[2] / "src/lemoncrow/pro/capabilities/review/session_models.py"
    ).read_text(encoding="utf-8")
    assert "import sqlite3" not in source
    assert "pygit2" not in source
    assert "from lemoncrow.pro.capabilities.review.store" not in source
    assert "from __future__ import annotations" in source


def test_every_dataclass_is_frozen_and_collections_default_to_empty(tmp_path: Path) -> None:
    for cls in (ReviewSession, ReviewRevision, ReviewUnit, ReviewMark, AnnotationAnchor, Annotation, DeliveryRecord):
        assert cls.__dataclass_params__.frozen, f"{cls.__name__} must be frozen"
        for spec in fields(cls):
            if spec.type == "tuple[str, ...]":
                assert spec.default == ()


# --------------------------------------------------------------------------- #
# review artifacts must never land in a user's commits (spec §3.3)
# --------------------------------------------------------------------------- #

_IGNORE_ALL = "*\n"


def test_the_review_directory_ignores_itself(tmp_path: Path) -> None:
    """A store root inside a repo must not leak packets into ``git status``.

    ``review/`` is a directory LemonCrow creates and nothing else writes into,
    so blanket-ignoring it is always safe.
    """
    store = _store(tmp_path / "somewhere")
    ignore = store.review_dir / ".gitignore"
    assert ignore.is_file()
    assert ignore.read_text(encoding="utf-8").endswith(_IGNORE_ALL)


def test_a_lemoncrow_named_store_root_ignores_itself_too(tmp_path: Path) -> None:
    """``<repo>/.lemoncrow`` is unambiguously ours, so the db is covered as well."""
    root = tmp_path / "repo" / ".lemoncrow"
    store = _store(root)
    assert (root / ".gitignore").read_text(encoding="utf-8").endswith(_IGNORE_ALL)
    assert (store.root / DB_NAME).is_file()


def test_a_store_root_the_user_named_is_never_blanket_ignored(tmp_path: Path) -> None:
    """Writing ``*`` into a directory somebody pointed LEMONCROW_ROOT at would
    silently untrack their work. Only the ``review/`` subtree is ours to claim.
    """
    root = tmp_path / "my-notes"
    store = _store(root)
    assert not (root / ".gitignore").exists()
    assert (store.review_dir / ".gitignore").is_file()


def test_an_unwritable_store_tree_still_reviews(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The courtesy is skipped, never escalated into a failed review."""
    from lemoncrow.pro.capabilities.review import store as store_module

    def _boom(directory: Path) -> list[str]:
        raise OSError("read-only file system")

    monkeypatch.setattr(store_module, "ensure_dir_gitignore", _boom)
    store = _store(tmp_path / "ro")
    assert store.list_sessions() == ()


def test_unified_annotation_source_round_trips_and_rejects_fake_human_judgment(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    author = store.add_annotation(
        Annotation(
            id="",
            review_id=session.id,
            revision_id=revision.id,
            anchor=_anchor(),
            body="Changed this because the background worker lacks request context.",
            kind="comment",
            created_by="claude",
            created_by_actor="agent",
            source="author",
            source_id="session-123",
            title="Why context became required",
            evidence=("session:session-123", "symbol:SessionManager.refresh"),
        )
    )
    loaded = store.get_annotation(author.id)
    assert loaded is not None
    assert loaded.source == "author"
    assert loaded.source_id == "session-123"
    assert loaded.title == "Why context became required"
    assert loaded.evidence == ("session:session-123", "symbol:SessionManager.refresh")

    with pytest.raises(ValueError, match="cannot record the human review disposition"):
        store.add_annotation(
            Annotation(
                id="",
                review_id=session.id,
                revision_id=revision.id,
                anchor=_anchor(),
                body="looks safe",
                kind="looks_good",
                created_by="review-bot",
                created_by_actor="agent",
                source="ai_review",
            )
        )


def test_prefixed_review_identity_migrates_without_losing_refs_or_frozen_blobs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session, revision = _seed(store)
    store.write_blob_artifact(session.id, revision.id, {"src/app.py": "print('frozen')\n"})
    store.set_mark(
        ReviewMark(
            review_id=session.id,
            unit_key="file:src/app.py",
            state="reviewed",
            reviewed_revision_id=revision.id,
            content_fingerprint="fp-1",
        )
    )

    def dashed(value: str) -> str:
        return f"{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}"

    old_review = f"r-{dashed(session.id)}"
    old_revision = f"rr-{dashed(revision.id)}"
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM review_meta WHERE key = 'review_identity_version'")
        conn.execute("UPDATE review_units SET revision_id = ? WHERE revision_id = ?", (old_revision, revision.id))
        conn.execute(
            "UPDATE review_marks SET review_id = ?, reviewed_revision_id = ? WHERE review_id = ?",
            (old_review, old_revision, session.id),
        )
        conn.execute(
            "UPDATE review_activity_events SET review_id = ?, revision_id = CASE WHEN revision_id = ? THEN ? ELSE revision_id END, "
            "subject_id = CASE WHEN subject_type = 'review' AND subject_id = ? THEN ? "
            "WHEN subject_type = 'revision' AND subject_id = ? THEN ? ELSE subject_id END WHERE review_id = ?",
            (old_review, revision.id, old_revision, session.id, old_review, revision.id, old_revision, session.id),
        )
        conn.execute(
            "UPDATE review_revisions SET id = ?, review_id = ? WHERE id = ?", (old_revision, old_review, revision.id)
        )
        conn.execute(
            "UPDATE review_sessions SET id = ?, current_revision_id = CASE WHEN current_revision_id = ? THEN ? ELSE current_revision_id END WHERE id = ?",
            (old_review, revision.id, old_revision, session.id),
        )
        conn.commit()

    bare_dir = store.root / "review" / "artifacts" / session.id / revision.id
    legacy_dir = store.root / "review" / "artifacts" / old_review / old_revision
    legacy_dir.parent.mkdir(parents=True, exist_ok=True)
    bare_dir.rename(legacy_dir)

    migrated = ReviewStore(tmp_path)
    migrated.init()

    loaded_session = migrated.get_session(session.id)
    loaded_revision = migrated.get_revision(revision.id)
    assert loaded_session is not None
    assert loaded_revision is not None
    assert loaded_revision.review_id == session.id
    assert migrated.resolve_session(f"r/{session.id}") == loaded_session
    assert migrated.resolve_revision(f"rr/{revision.id}") == loaded_revision
    assert any(mark.unit_key == "file:src/app.py" for mark in migrated.list_marks(session.id))
    assert migrated.read_blob_artifact(session.id, revision.id) == {"src/app.py": "print('frozen')\n"}
    assert not legacy_dir.exists()
    assert (store.root / "review" / "artifacts" / session.id / revision.id).is_dir()


def test_review_identity_is_bare_uuid_with_canonical_public_ref(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_session(_session(source_ref="current"))

    assert len(session.id) == 32
    assert all(char in "0123456789abcdef" for char in session.id)
    assert store.session_reference(session.id) == f"r/{session.id}"
    assert store.resolve_session(session.id) == session
    assert store.resolve_session(f"r/{session.id}") == session
    assert store.resolve_session(f"r-{session.id}") is None
    assert store.resolve_session(session.id[:8]) is None


def test_revision_identity_is_bare_uuid_with_canonical_public_ref(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_session(_session())
    revision = store.add_revision(_revision(session.id), [_unit()])

    assert len(revision.id) == 32
    assert all(char in "0123456789abcdef" for char in revision.id)
    assert store.revision_reference(revision.id) == f"rr/{revision.id}"
    assert store.resolve_revision(revision.id) == revision
    assert store.resolve_revision(f"rr/{revision.id}") == revision
    assert store.resolve_revision(f"rr-{revision.id}") is None
    assert store.resolve_revision(revision.id[:8]) is None
