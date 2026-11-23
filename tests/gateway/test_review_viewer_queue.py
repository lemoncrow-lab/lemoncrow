"""Per-viewer queue state (P5-1): the snooze and resume rules, and the store rows behind them."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.models import SCHEMA_VERSION
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession, ReviewUnit
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.viewer_queue import (
    Resume,
    ViewerState,
    ViewerStateInvalid,
    parse_resume,
    parse_snooze_until,
    resume_from_json,
    resume_to_json,
    snooze_active,
)

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)


def _iso(delta: timedelta) -> str:
    return (NOW + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- the pure rules ---------------------------------------------------------------


def test_a_snooze_holds_until_it_is_due_and_then_ends_by_itself() -> None:
    state = ViewerState("r1", "local", snoozed_until=_iso(timedelta(hours=1)), snoozed_revision=0)
    assert snooze_active(state, current_revision=0, now=NOW)
    assert snooze_active(state, current_revision=0, now=NOW + timedelta(minutes=59))
    assert not snooze_active(state, current_revision=0, now=NOW + timedelta(hours=1))
    assert not snooze_active(state, current_revision=0, now=NOW + timedelta(hours=2))


def test_a_newer_snapshot_ends_a_snooze_early() -> None:
    state = ViewerState("r1", "local", snoozed_until=_iso(timedelta(days=3)), snoozed_revision=0)
    assert snooze_active(state, current_revision=0, now=NOW)
    assert not snooze_active(state, current_revision=1, now=NOW)


def test_no_row_or_an_unreadable_time_is_not_a_snooze() -> None:
    assert not snooze_active(None, current_revision=0, now=NOW)
    assert not snooze_active(ViewerState("r1", "local"), current_revision=0, now=NOW)
    assert not snooze_active(ViewerState("r1", "local", snoozed_until="tomorrow"), current_revision=0, now=NOW)


def test_the_queue_wire_hides_a_snooze_that_no_longer_holds_but_keeps_the_place() -> None:
    resume = Resume("one", "can_view_order", 2, 2)
    state = ViewerState("r1", "local", _iso(timedelta(hours=1)), 0, resume)
    assert state.queue_wire(current_revision=0, now=NOW)["snoozed_until"] == _iso(timedelta(hours=1))
    expired = state.queue_wire(current_revision=0, now=NOW + timedelta(hours=2))
    assert expired == {"snoozed_until": "", "resume": resume.to_wire()}


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "tomorrow",
        "2026-11-07T11:00:00",
        _iso(timedelta(seconds=-1)),
        _iso(timedelta(0)),
        _iso(timedelta(days=32)),
        7,
    ],
)
def test_a_snooze_time_must_be_aware_future_and_within_31_days(raw: object) -> None:
    with pytest.raises(ViewerStateInvalid) as caught:
        parse_snooze_until(raw, now=NOW)
    assert caught.value.field == "snoozed_until"


def test_a_good_snooze_time_is_normalised_to_utc_seconds() -> None:
    assert parse_snooze_until("2026-11-07T13:30:00+02:00", now=NOW) == "2026-11-07T11:30:00Z"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "one",
        {"screen": "overview"},
        {"screen": "one", "position": 3, "total": 2},
        {"screen": "one", "position": True, "total": 2},
        {"screen": "one", "position": -1, "total": 2},
        {"screen": "one", "part": "x" * 401},
    ],
)
def test_a_resume_position_names_a_screen_and_stays_inside_its_total(raw: object) -> None:
    with pytest.raises(ViewerStateInvalid):
        parse_resume(raw)


def test_a_resume_position_round_trips_and_a_damaged_row_reads_as_none() -> None:
    resume = parse_resume({"screen": "quick_check", "part": "order_total", "position": 1, "total": 1})
    assert resume_from_json(resume_to_json(resume)) == resume
    assert resume_from_json("") is None
    assert resume_from_json("{not json") is None
    assert resume_from_json('{"screen": "somewhere"}') is None


# -- the store rows ---------------------------------------------------------------


def _store(tmp_path: Path) -> ReviewStore:
    store = ReviewStore(tmp_path)
    store.init()
    return store


def _review(store: ReviewStore, title: str = "can_view_order") -> tuple[str, str]:
    session = store.create_session(
        ReviewSession(id="", subject_type="local_change", repo_root="/srv/q", range_mode="working_tree", title=title)
    )
    revision = store.add_revision(
        ReviewRevision(
            id="",
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint=f"tree-{title}",
            packet_schema_version=SCHEMA_VERSION,
        ),
        [
            ReviewUnit(
                revision_id="",
                unit_key=f"file:{title}.py",
                kind="file",
                path=f"{title}.py",
                content_fingerprint=f"fp-{title}",
                fingerprint_method="blob_sha256",
            )
        ],
    )
    store.set_current_revision(session.id, revision.id)
    return session.id, revision.id


def _future(hours: int = 1) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(store: ReviewStore) -> int:
    with sqlite3.connect(store.db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM review_viewer_state").fetchone()[0])


def test_snooze_and_resume_are_kept_per_viewer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    review_id, _ = _review(store)
    until = _future()
    store.set_viewer_state(review_id, "maya", snooze=until, resume=Resume("one", "a", 1, 2))
    mine = store.viewer_states([review_id], "maya")[review_id]
    assert (mine.snoozed_until, mine.resume) == (until, Resume("one", "a", 1, 2))
    assert store.viewer_states([review_id], "sam") == {}


def test_leaving_a_part_out_keeps_it_and_none_clears_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    review_id, _ = _review(store)
    until = _future()
    store.set_viewer_state(review_id, "maya", snooze=until)
    store.set_viewer_state(review_id, "maya", resume=Resume("wrap"))
    kept = store.viewer_states([review_id], "maya")[review_id]
    assert kept.snoozed_until == until and kept.resume == Resume("wrap")
    store.set_viewer_state(review_id, "maya", snooze=None)
    assert store.viewer_states([review_id], "maya")[review_id].snoozed_until == ""


def test_a_row_with_neither_part_is_deleted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    review_id, _ = _review(store)
    store.set_viewer_state(review_id, "maya", snooze=_future())
    assert _rows(store) == 1
    assert store.set_viewer_state(review_id, "maya", snooze=None) is None
    assert _rows(store) == 0


def test_a_snooze_is_pinned_to_the_snapshot_it_was_set_on(tmp_path: Path) -> None:
    store = _store(tmp_path)
    review_id, _ = _review(store)
    saved = store.set_viewer_state(review_id, "maya", snooze=_future())
    first = store.latest_revision(review_id)
    assert first is not None and saved is not None and saved.snoozed_revision == first.revision_number
    second = store.add_revision(
        ReviewRevision(
            id="",
            review_id=review_id,
            revision_number=first.revision_number + 1,
            range_mode="working_tree",
            tree_fingerprint="tree-2",
            packet_schema_version=SCHEMA_VERSION,
        ),
        [
            ReviewUnit(
                revision_id="",
                unit_key="file:b.py",
                kind="file",
                path="b.py",
                content_fingerprint="fp-b",
                fingerprint_method="blob_sha256",
            )
        ],
    )
    store.set_current_revision(review_id, second.id)
    pinned = store.viewer_states([review_id], "maya")[review_id]
    newest = store.latest_revision(review_id)
    assert newest is not None and newest.revision_number > first.revision_number
    assert snooze_active(pinned, current_revision=first.revision_number, now=datetime.now(UTC))
    assert not snooze_active(pinned, current_revision=newest.revision_number, now=datetime.now(UTC))


def test_every_write_drops_that_viewers_expired_snoozes_and_their_empty_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first, _ = _review(store, "first")
    second, _ = _review(store, "second")
    store.set_viewer_state(first, "maya", snooze=_future())
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE review_viewer_state SET snoozed_until = '2020-01-01T00:00:00Z'")
    assert _rows(store) == 1
    store.set_viewer_state(second, "maya", resume=Resume("wrap"))
    assert store.viewer_states([first], "maya") == {}
    assert _rows(store) == 1


def test_deleting_a_review_deletes_its_queue_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    review_id, _ = _review(store)
    store.set_viewer_state(review_id, "maya", snooze=_future(), resume=Resume("wrap"))
    store.set_viewer_state(review_id, "sam", resume=Resume("wrap"))
    assert _rows(store) == 2
    store.update_session(review_id, status="archived")
    assert store.delete_archived_session(review_id)
    assert _rows(store) == 0


def test_an_unknown_review_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.set_viewer_state("rv_missing", "maya", snooze=_future())
