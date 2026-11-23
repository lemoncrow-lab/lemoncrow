"""Local <-> hosted Review attachment (G5-SYNC): store, queue guards, conflict rules, reconnect, Reader API.

The hosted half (projection, replay, policy, the end-to-end scenario) is proven
under ``enterprise/server/tests/test_hosted_review_attachment.py``; nothing here
reads ``enterprise/``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review import api as review_api
from lemoncrow.pro.capabilities.review import hosted_attachment as engine
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.errors import (
    HostedActionRefused,
    HostedAttachmentConflict,
    HostedReplayRefused,
    HostedUnavailable,
)
from lemoncrow.pro.capabilities.review.hosted_attachment import (
    COMMENT_BODY_LIMIT,
    HostedFacts,
    anchor_fingerprint,
    conflict_reason,
    parse_projection,
    prepare_action,
    reconnect,
    requeue_keeps_content,
)
from lemoncrow.pro.capabilities.review.session_models import (
    Annotation,
    AnnotationAnchor,
    HostedAction,
    HostedAttachment,
    ReviewMark,
    ReviewRevision,
    ReviewSession,
    ReviewUnit,
)
from lemoncrow.pro.capabilities.review.store import (
    ReviewStore,
    new_annotation_id,
    new_hosted_action_id,
    new_revision_id,
    new_session_id,
)
from lemoncrow.pro.capabilities.review.units import file_fingerprint

TOKEN = "test-workspace-token"
PORT = 45998
BASE = f"http://127.0.0.1:{PORT}"
HOSTED_ID = "0199aaaabbbbccccddddeeeeffff0000"
ORIGIN = "https://hosted.example"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _units() -> list[ReviewUnit]:
    return [
        ReviewUnit(revision_id="", unit_key="file:src/a.py", kind="file", path="src/a.py", content_fingerprint="fp-a"),
        ReviewUnit(revision_id="", unit_key="file:src/b.py", kind="file", path="src/b.py", content_fingerprint="fp-b"),
        ReviewUnit(
            revision_id="",
            unit_key="symbol:src/b.py:beta",
            kind="symbol",
            path="src/b.py",
            content_fingerprint="fp-beta",
            symbol="beta",
        ),
    ]


def _seed(store: ReviewStore, repo_root: Path) -> tuple[ReviewSession, ReviewRevision]:
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root=str(repo_root.resolve()),
            range_mode="working_tree",
            title="attached change",
            source_ref="",
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=1,
            range_mode="working_tree",
            tree_fingerprint="tree-1",
            packet_schema_version=1,
        ),
        _units(),
    )
    # The exact new side the reviewer sees: a queued comment anchors on it (RC5).
    store.write_blob_artifact(session.id, revision.id, _LOCAL_TEXT)
    return session, revision


def _projection(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "reference": f"r/{HOSTED_ID}",
        "review_id": HOSTED_ID,
        "repo_id": "hosted-repo",
        "principal_id": "alice",
        "status": "open",
        "snapshot": {"id": "hs-1", "number": 0},
        "targets": {
            "file:src/a.py": {"path": "src/a.py", "state": "unreviewed", "fingerprint": "fp-a"},
            "symbol:src/b.py:beta": {"path": "src/b.py", "state": "unreviewed", "fingerprint": "fp-beta"},
            "file:src/c.py": {"path": "src/c.py", "state": "unreviewed", "fingerprint": "fp-c"},
        },
        "files": {"src/a.py": "fp-a", "src/b.py": "fp-b-hosted", "src/c.py": "fp-c"},
        "threads": {"ann-t1": {"path": "src/a.py", "state": "open", "version": 2}},
        "my_marks": {"file:src/a.py": {"state": "needs_changes", "reviewed_revision_id": "hs-0"}},
        "my_outcome": {"id": "rot-1", "outcome": "comment", "revision_id": "hs-0"},
        "my_requests": {
            "req-1": {"state": "active", "version": 3, "scope_kind": "all"},
            "req-done": {"state": "completed", "version": 4, "scope_kind": "all"},
        },
        "policy": {"configured": True, "ready": False, "blocking": ["reviewer alice has not completed"]},
    }
    base.update(overrides)
    return base


_LOCAL_UNITS = {"file:src/a.py": "fp-a", "file:src/b.py": "fp-b", "symbol:src/b.py:beta": "fp-beta"}
_LOCAL_FILES = {"src/a.py": "fp-a", "src/b.py": "fp-b"}
_LOCAL_TEXT = {"src/a.py": "alpha = 1\nalpha += 1\nprint(alpha)\n", "src/b.py": "def beta():\n    return 2\n"}
_ANCHOR_A = anchor_fingerprint("src/a.py", _LOCAL_TEXT["src/a.py"])


def _identical(**overrides: Any) -> dict[str, Any]:
    """A hosted projection whose Snapshot is exactly the local revision's content."""

    base: dict[str, Any] = {
        "targets": {
            "file:src/a.py": {"path": "src/a.py", "state": "unreviewed", "fingerprint": "fp-a"},
            "symbol:src/b.py:beta": {"path": "src/b.py", "state": "unreviewed", "fingerprint": "fp-beta"},
        },
        "files": dict(_LOCAL_FILES),
    }
    return _projection(**{**base, **overrides})


def _prepare(
    kind: str,
    body: Mapping[str, Any],
    projection: Mapping[str, Any] | None = None,
    *,
    text: Mapping[str, str] = _LOCAL_TEXT,
) -> engine.PreparedAction:
    return prepare_action(
        kind,
        body,
        projection if projection is not None else _projection(),
        local_units=_LOCAL_UNITS,
        local_files=_LOCAL_FILES,
        local_text=text.get,
    )


def _attachment(review_id: str, **overrides: Any) -> HostedAttachment:
    base = HostedAttachment(
        review_id=review_id,
        origin=ORIGIN,
        hosted_repo_id="hosted-repo",
        hosted_review_id=HOSTED_ID,
        principal_id="alice",
        projection_json=json.dumps(_projection()),
    )
    return HostedAttachment(**{**base.__dict__, **overrides})


def _action(review_id: str, kind: str = "comment", *, key: str = "", action_id: str = "") -> HostedAction:
    return HostedAction(
        id=action_id or new_hosted_action_id(),
        review_id=review_id,
        kind=kind,  # type: ignore[arg-type]
        snapshot_id="hs-1",
        payload_json="{}",
        preconditions_json="{}",
        supersede_key=key,
    )


@pytest.fixture
def store(tmp_path: Path) -> ReviewStore:
    store = ReviewStore(tmp_path / "store")
    store.init()
    return store


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


# --------------------------------------------------------------------------- #
# store: explicit attachment, one identity
# --------------------------------------------------------------------------- #


def test_attach_is_explicit_idempotent_and_one_local_review_per_hosted_review(
    store: ReviewStore, repo_root: Path
) -> None:
    session, _revision = _seed(store, repo_root)
    other, _ = _seed(store, repo_root)
    assert store.hosted_attachment_supported() is True
    assert store.get_hosted_attachment(session.id) is None

    stored, created = store.attach_hosted(_attachment(session.id))
    assert created is True
    assert (stored.hosted_review_id, stored.principal_id, stored.origin) == (HOSTED_ID, "alice", ORIGIN)
    assert stored.synced_at and stored.attached_at

    refreshed, created_again = store.attach_hosted(_attachment(session.id, projection_json='{"status": "open"}'))
    assert created_again is False
    assert refreshed.projection_json == '{"status": "open"}'
    assert refreshed.attached_at == stored.attached_at

    with pytest.raises(HostedAttachmentConflict) as other_target:
        store.attach_hosted(_attachment(session.id, hosted_review_id="another-hosted-review"))
    assert other_target.value.code == "already_attached"
    with pytest.raises(HostedAttachmentConflict) as other_principal:
        store.attach_hosted(_attachment(session.id, principal_id="bob"))
    assert other_principal.value.code == "already_attached"
    with pytest.raises(HostedAttachmentConflict) as taken:
        store.attach_hosted(_attachment(other.id))
    assert taken.value.code == "hosted_review_taken"
    # The hosted id is the Review's one identity here (inbox rows, routes): the
    # same id from another origin or repository would show twice, so it is refused.
    for elsewhere in ({"origin": "https://other.example"}, {"hosted_repo_id": "other-repo"}):
        with pytest.raises(HostedAttachmentConflict) as shared:
            store.attach_hosted(_attachment(other.id, **elsewhere))
        assert shared.value.code == "hosted_review_taken"
    other_id, _ = store.attach_hosted(_attachment(other.id, hosted_review_id="another-hosted-review"))
    assert other_id.review_id == other.id

    with pytest.raises(ValueError, match="needs origin"):
        store.attach_hosted(_attachment(session.id, principal_id=" "))
    with pytest.raises(ValueError, match="no review"):
        store.attach_hosted(_attachment("0199ffffffffffffffffffffffffffff"))
    assert set(store.hosted_attachments()) == {session.id, other.id}


def test_the_hosted_reference_resolves_to_the_attached_local_review(store: ReviewStore, repo_root: Path) -> None:
    session, _revision = _seed(store, repo_root)
    assert store.resolve_session(HOSTED_ID) is None
    store.attach_hosted(_attachment(session.id))
    resolved = store.resolve_session(HOSTED_ID)
    assert resolved is not None and resolved.id == session.id
    by_ref = store.resolve_session(f"r/{HOSTED_ID}")
    assert by_ref is not None and by_ref.id == session.id
    attached = store.find_hosted_attachment(HOSTED_ID)
    assert attached is not None and attached.review_id == session.id
    # The local id keeps routing.
    local = store.resolve_session(session.id)
    assert local is not None and local.id == session.id


def _attach_bypassing_the_store(store: ReviewStore, review_id: str, origin: str) -> None:
    """A row the store itself now refuses (a second origin's same hosted id), as older data could hold."""

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO review_hosted_attachments (review_id, origin, hosted_repo_id, hosted_review_id,"
            " principal_id, projection_json, attached_at, synced_at) VALUES (?, ?, ?, ?, ?, '{}', 'now', 'now')",
            (review_id, origin, "hosted-repo", HOSTED_ID, "alice"),
        )


def test_a_hosted_id_attached_on_two_origins_is_an_ambiguous_reference(store: ReviewStore, repo_root: Path) -> None:
    from lemoncrow.pro.capabilities.review.store import AmbiguousReviewReference

    first, _ = _seed(store, repo_root)
    second, _ = _seed(store, repo_root)
    store.attach_hosted(_attachment(first.id))
    _attach_bypassing_the_store(store, second.id, "https://other.example")
    with pytest.raises(AmbiguousReviewReference):
        store.resolve_session(HOSTED_ID)


# --------------------------------------------------------------------------- #
# store: the queue is bounded and closed
# --------------------------------------------------------------------------- #


def test_the_queue_refuses_an_action_past_the_unresolved_bound(store: ReviewStore, repo_root: Path) -> None:
    session, _ = _seed(store, repo_root)
    with pytest.raises(HostedAttachmentConflict) as unattached:
        store.add_hosted_action(_action(session.id), unresolved_limit=3)
    assert unattached.value.code == "not_attached"
    store.attach_hosted(_attachment(session.id))
    stored = [store.add_hosted_action(_action(session.id), unresolved_limit=3)[0] for _ in range(3)]
    assert [row.sequence for row in stored] == [1, 2, 3]
    assert all(row.state == "pending" for row in stored)

    with pytest.raises(HostedAttachmentConflict) as full:
        store.add_hosted_action(_action(session.id), unresolved_limit=3)
    assert full.value.code == "queue_full"

    # A conflict still needs the reviewer: it counts against the bound.
    store.transition_hosted_action(
        session.id, stored[0].id, expected_state="pending", state="conflict", reason="snapshot_advanced"
    )
    with pytest.raises(HostedAttachmentConflict):
        store.add_hosted_action(_action(session.id), unresolved_limit=3)
    # Applied and discarded do not.
    store.transition_hosted_action(session.id, stored[1].id, expected_state="pending", state="applied")
    fourth, _ = store.add_hosted_action(_action(session.id), unresolved_limit=3)
    assert fourth.sequence == 4
    with pytest.raises(ValueError):
        store.add_hosted_action(_action(session.id), unresolved_limit=0)
    with pytest.raises(ValueError):
        store.add_hosted_action(_action(session.id, kind="evidence"), unresolved_limit=3)


def test_a_newer_judgment_supersedes_and_a_duplicate_request_completion_is_refused(
    store: ReviewStore, repo_root: Path
) -> None:
    session, _ = _seed(store, repo_root)
    store.attach_hosted(_attachment(session.id))
    first, _ = store.add_hosted_action(_action(session.id, "mark", key="mark:file:src/a.py"), unresolved_limit=10)
    store.transition_hosted_action(
        session.id, first.id, expected_state="pending", state="conflict", reason="snapshot_advanced"
    )
    second, superseded = store.add_hosted_action(
        _action(session.id, "mark", key="mark:file:src/a.py"), unresolved_limit=10, supersede=True
    )
    assert superseded == (first.id,)
    old = store.get_hosted_action(session.id, first.id)
    assert old is not None and (old.state, old.reason) == ("discarded", "superseded")
    assert json.loads(old.result_json) == {"superseded_by": second.id}
    other_unit, none_superseded = store.add_hosted_action(
        _action(session.id, "mark", key="mark:file:src/b.py"), unresolved_limit=10, supersede=True
    )
    assert none_superseded == ()

    store.add_hosted_action(_action(session.id, "request_complete", key="request:req-1"), unresolved_limit=10)
    with pytest.raises(HostedAttachmentConflict) as duplicate:
        store.add_hosted_action(_action(session.id, "request_complete", key="request:req-1"), unresolved_limit=10)
    assert duplicate.value.code == "duplicate_action"
    # Unkeyed actions (comments) never collide.
    store.add_hosted_action(_action(session.id), unresolved_limit=10)
    store.add_hosted_action(_action(session.id), unresolved_limit=10)
    assert other_unit.state == "pending"


def test_transitions_are_compare_and_set(store: ReviewStore, repo_root: Path) -> None:
    session, _ = _seed(store, repo_root)
    store.attach_hosted(_attachment(session.id))
    action, _ = store.add_hosted_action(_action(session.id), unresolved_limit=10)
    assert action.resolved_at == ""
    with pytest.raises(HostedAttachmentConflict) as lost:
        store.transition_hosted_action(session.id, action.id, expected_state="conflict", state="applied")
    assert lost.value.code == "action_state_changed"
    with pytest.raises(HostedAttachmentConflict) as unknown:
        store.transition_hosted_action(session.id, "rha-missing", expected_state="pending", state="applied")
    assert unknown.value.code == "action_unknown"
    for wrong in ("merged", "pending"):
        with pytest.raises(ValueError):
            store.transition_hosted_action(session.id, action.id, expected_state="pending", state=wrong)
    with pytest.raises(ValueError):
        store.transition_hosted_action(session.id, action.id, expected_state="merged", state="applied")
    applied = store.transition_hosted_action(
        session.id, action.id, expected_state="pending", state="applied", result_json='{"ok": 1}'
    )
    assert (applied.state, applied.result_json) == ("applied", '{"ok": 1}')
    assert applied.resolved_at


def test_prune_closes_resolved_actions_by_count_and_age_and_never_unresolved(
    store: ReviewStore, repo_root: Path
) -> None:
    session, _ = _seed(store, repo_root)
    store.attach_hosted(_attachment(session.id))
    rows = [store.add_hosted_action(_action(session.id), unresolved_limit=10)[0] for _ in range(5)]
    resolved = [
        store.transition_hosted_action(session.id, row.id, expected_state="pending", state=state)
        for row, state in zip(rows[:3], ("applied", "discarded", "applied"), strict=True)
    ]
    store.transition_hosted_action(
        session.id, rows[3].id, expected_state="pending", state="conflict", reason="snapshot_advanced"
    )

    # At the count bound nothing goes; one past it, the oldest resolved goes.
    assert store.prune_hosted_actions(session.id, keep_resolved=3, resolved_before="") == 0
    assert store.prune_hosted_actions(session.id, keep_resolved=2, resolved_before="") == 1
    assert store.get_hosted_action(session.id, resolved[0].id) is None
    # The age bound is strict: a row resolved exactly at the cutoff stays.
    assert store.prune_hosted_actions(session.id, keep_resolved=10, resolved_before=resolved[1].resolved_at) == 0
    assert store.prune_hosted_actions(session.id, keep_resolved=10, resolved_before="9999") == 2
    left = store.list_hosted_actions(session.id)
    assert [(row.id, row.state) for row in left] == [(rows[3].id, "conflict"), (rows[4].id, "pending")]
    assert store.prune_hosted_actions(session.id, keep_resolved=0, resolved_before="9999") == 0
    with pytest.raises(ValueError):
        store.prune_hosted_actions(session.id, keep_resolved=-1, resolved_before="")
    assert [row.id for row in store.list_hosted_actions(session.id, states=("pending",))] == [rows[4].id]


def test_detach_refuses_unsent_work_unless_discarded_and_keeps_local_review_state(
    store: ReviewStore, repo_root: Path
) -> None:
    session, revision = _seed(store, repo_root)
    # Reviewed locally before attaching: stays local, never copied anywhere.
    store.set_mark(
        ReviewMark(
            review_id=session.id,
            unit_key="file:src/a.py",
            state="reviewed",
            reviewed_revision_id=revision.id,
            content_fingerprint="fp-a",
        )
    )
    store.record_outcome(session.id, revision.id, "local", outcome="lgtm")
    with pytest.raises(HostedAttachmentConflict) as unattached:
        store.detach_hosted(session.id)
    assert unattached.value.code == "not_attached"

    store.attach_hosted(_attachment(session.id))
    pending, _ = store.add_hosted_action(_action(session.id), unresolved_limit=10)
    applied, _ = store.add_hosted_action(_action(session.id), unresolved_limit=10)
    store.transition_hosted_action(session.id, applied.id, expected_state="pending", state="applied")
    with pytest.raises(HostedAttachmentConflict) as unsent:
        store.detach_hosted(session.id)
    assert unsent.value.code == "unresolved_actions"
    assert store.get_hosted_attachment(session.id) is not None

    assert store.detach_hosted(session.id, discard_unresolved=True) == (1, 1)
    assert store.get_hosted_attachment(session.id) is None
    assert store.list_hosted_actions(session.id) == ()
    assert store.get_hosted_action(session.id, pending.id) is None
    assert [mark.unit_key for mark in store.list_marks(session.id)] == ["file:src/a.py"]
    kept_outcome = store.latest_outcome(session.id, "local")
    assert kept_outcome is not None and kept_outcome.outcome == "lgtm"


def test_detach_without_unresolved_work_needs_no_discard(store: ReviewStore, repo_root: Path) -> None:
    session, _ = _seed(store, repo_root)
    store.attach_hosted(_attachment(session.id))
    assert store.detach_hosted(session.id) == (0, 0)


def test_review_deletion_refuses_unsent_hosted_work_then_cascades(store: ReviewStore, repo_root: Path) -> None:
    session, _ = _seed(store, repo_root)
    store.attach_hosted(_attachment(session.id))
    pending, _ = store.add_hosted_action(_action(session.id), unresolved_limit=10)
    conflicted, _ = store.add_hosted_action(_action(session.id), unresolved_limit=10)
    applied, _ = store.add_hosted_action(_action(session.id), unresolved_limit=10)
    store.transition_hosted_action(
        session.id, conflicted.id, expected_state="pending", state="conflict", reason="snapshot_advanced"
    )
    store.transition_hosted_action(session.id, applied.id, expected_state="pending", state="applied")
    store.update_session(session.id, status="archived")
    # Pending and conflicted work each refuse: neither is dropped by the cascade.
    for unsent, was in ((pending, "pending"), (conflicted, "conflict")):
        with pytest.raises(HostedAttachmentConflict) as refused:
            store.delete_archived_session(session.id)
        assert refused.value.code == "unresolved_actions"
        assert store.get_session(session.id) is not None
        assert store.get_hosted_attachment(session.id) is not None
        assert len(store.list_hosted_actions(session.id)) == 3
        store.transition_hosted_action(session.id, unsent.id, expected_state=was, state="discarded")
    assert store.delete_archived_session(session.id)
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_hosted_attachments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM review_hosted_actions").fetchone()[0] == 0


def test_the_schema_itself_holds_one_local_review_per_hosted_review(store: ReviewStore, repo_root: Path) -> None:
    session, _ = _seed(store, repo_root)
    other, _ = _seed(store, repo_root)
    store.attach_hosted(_attachment(session.id))
    with sqlite3.connect(store.db_path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO review_hosted_attachments"
            " (review_id, origin, hosted_repo_id, hosted_review_id, principal_id, attached_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (other.id, ORIGIN, "hosted-repo", HOSTED_ID, "bob", "2026-10-06T00:00:00+00:00"),
        )


# --------------------------------------------------------------------------- #
# engine: queue guards
# --------------------------------------------------------------------------- #


def test_a_judgment_queues_only_on_content_identical_to_the_hosted_snapshot() -> None:
    prepared = _prepare("mark", {"unit_key": "file:src/a.py", "state": "reviewed"})
    assert prepared.snapshot_id == "hs-1"
    assert prepared.payload == {"unit_key": "file:src/a.py", "state": "reviewed", "fingerprint": "fp-a"}
    # Hosted confirms the judged unit is exactly this content on its Snapshot (RC5).
    assert prepared.preconditions == {"mark": ["needs_changes", "hs-0"], "fingerprint": "fp-a"}
    assert (prepared.supersede_key, prepared.supersede) == ("mark:file:src/a.py", True)
    unjudged = _prepare("mark", {"unit_key": "symbol:src/b.py:beta"})
    assert unjudged.preconditions == {"mark": None, "fingerprint": "fp-beta"}
    assert unjudged.payload["state"] == "reviewed"

    cases = {
        "file:src/b.py": "unknown_target",  # local only: not a hosted target
        "file:src/c.py": "unknown_target",  # hosted only: the reviewer never saw it
    }
    for unit_key, code in cases.items():
        with pytest.raises(HostedActionRefused) as refused:
            _prepare("mark", {"unit_key": unit_key})
        assert refused.value.code == code, unit_key
    drifted = _projection()
    drifted["targets"]["file:src/a.py"]["fingerprint"] = "fp-a-hosted"
    with pytest.raises(HostedActionRefused) as mismatch:
        _prepare("mark", {"unit_key": "file:src/a.py"}, drifted)
    assert mismatch.value.code == "content_mismatch"
    for state in ("approved", 7, False):
        with pytest.raises(HostedActionRefused) as bad_state:
            _prepare("mark", {"unit_key": "file:src/a.py", "state": state})
        assert bad_state.value.code == "invalid", state


def test_a_comment_queues_only_on_a_file_identical_to_the_hosted_snapshot() -> None:
    prepared = _prepare("comment", {"path": "src/a.py", "body": "why?", "start_line": 2})
    assert prepared.payload == {
        "path": "src/a.py",
        "body": "why?",
        "kind": "comment",
        "side": "new",
        "file_level": False,
        "start_line": 2,
        "end_line": 2,
    }
    assert (prepared.preconditions, prepared.supersede_key, prepared.supersede) == (
        {"fingerprint": "fp-a", "anchor": _ANCHOR_A},
        "",
        False,
    )
    file_level = _prepare("comment", {"path": "src/a.py", "body": "x", "file_level": True, "start_line": 9})
    assert (file_level.payload["start_line"], file_level.payload["end_line"]) == (0, 0)
    at_limit = _prepare("comment", {"path": "src/a.py", "body": "x" * COMMENT_BODY_LIMIT, "file_level": True})
    assert len(at_limit.payload["body"]) == COMMENT_BODY_LIMIT

    refusals = [
        ({"path": "src/b.py", "body": "x", "file_level": True}, "content_mismatch"),
        ({"path": "src/c.py", "body": "x", "file_level": True}, "unknown_target"),
        ({"path": "src/zzz.py", "body": "x", "file_level": True}, "unknown_target"),
        ({"path": "src/a.py", "body": "  ", "file_level": True}, "invalid"),
        ({"path": "src/a.py", "body": "x" * (COMMENT_BODY_LIMIT + 1), "file_level": True}, "invalid"),
        ({"path": "src/a.py", "body": "x", "kind": "praise", "file_level": True}, "invalid"),
        ({"path": "src/a.py", "body": "x", "start_line": -1}, "invalid"),
        ({"path": "src/a.py", "body": "x", "start_line": True}, "invalid"),
        ({"body": "x"}, "invalid"),
        # The hosted comment endpoint's rules: a line comment names its line, nothing is cast.
        ({"path": "src/a.py", "body": "x"}, "invalid"),
        ({"path": "src/a.py", "body": "x", "start_line": "2"}, "invalid"),
        ({"path": "src/a.py", "body": "x", "start_line": 2.7}, "invalid"),
        ({"path": "src/a.py", "body": "x", "start_line": None}, "invalid"),
        ({"path": "src/a.py", "body": "x", "start_line": 2, "end_line": "3"}, "invalid"),
        ({"path": "src/a.py", "body": "x", "start_line": 2, "end_line": None}, "invalid"),
        ({"path": "src/a.py", "body": "x", "file_level": "false"}, "invalid"),
        ({"path": "src/a.py", "body": "x", "file_level": 1}, "invalid"),
        ({"path": "src/a.py", "body": "x", "file_level": True, "kind": 7}, "invalid"),
        ({"path": "src/a.py", "body": "x", "file_level": True, "side": 7}, "invalid"),
    ]
    local_missing = _projection(files={**_projection()["files"], "src/new.py": "fp-new"})
    with pytest.raises(HostedActionRefused) as missing:
        _prepare("comment", {"path": "src/new.py", "body": "x", "file_level": True}, local_missing)
    assert missing.value.code == "unknown_target"
    hosted_missing = _projection(files={"src/b.py": "fp-b"})
    with pytest.raises(HostedActionRefused) as not_hosted:
        _prepare("comment", {"path": "src/a.py", "body": "x", "file_level": True}, hosted_missing)
    assert not_hosted.value.code == "unknown_target"
    for body, code in refusals:
        with pytest.raises(HostedActionRefused) as refused:
            _prepare("comment", body)
        assert refused.value.code == code, body


def test_a_queued_comment_anchors_on_exact_new_side_text_or_is_online_only() -> None:
    text = _LOCAL_TEXT["src/a.py"]
    # Unit fingerprints forgive outer blank lines, trailing whitespace and line-ending noise
    # (a lone CR or form feed splits a line); line numbers do not, so neither does the anchor.
    variants = (
        "\n\n" + text,
        text.replace("\n", "\r\n"),
        text.replace("\n", " \n"),
        text.replace("\n", "\r", 1),
        text.replace("\n", "\x0c", 1),
    )
    for variant in variants:
        assert file_fingerprint("src/a.py", variant) == file_fingerprint("src/a.py", text), repr(variant)
        moved = _prepare("comment", {"path": "src/a.py", "body": "x", "start_line": 2}, text={"src/a.py": variant})
        assert moved.preconditions["anchor"] == anchor_fingerprint("src/a.py", variant) != _ANCHOR_A
    assert anchor_fingerprint("src/b.py", text) != _ANCHOR_A  # the path is part of the anchor

    online_only = [
        # Neither side keeps old-side text: a comment on a deletion cannot be confirmed later.
        ({"path": "src/a.py", "body": "x", "start_line": 2, "side": "old"}, _LOCAL_TEXT),
        ({"path": "src/a.py", "body": "x", "file_level": True, "side": "old"}, _LOCAL_TEXT),
        ({"path": "src/a.py", "body": "x", "start_line": 2, "side": "both"}, _LOCAL_TEXT),
        # A deleted, binary or uncaptured file has no exact new side to anchor on.
        ({"path": "src/a.py", "body": "x", "start_line": 2}, {}),
        ({"path": "src/a.py", "body": "x", "file_level": True}, {"src/b.py": "other"}),
    ]
    for body, local_text in online_only:
        with pytest.raises(HostedActionRefused) as refused:
            _prepare("comment", body, text=local_text)
        assert refused.value.code == "online_only", body
    with pytest.raises(HostedActionRefused) as no_reader:
        prepare_action(
            "comment",
            {"path": "src/a.py", "body": "x", "file_level": True},
            _projection(),
            local_units=_LOCAL_UNITS,
            local_files=_LOCAL_FILES,
        )
    assert no_reader.value.code == "online_only"


def test_a_verdict_queues_only_on_a_local_revision_identical_to_the_hosted_snapshot() -> None:
    outcome = _prepare("outcome", {"outcome": "lgtm"}, _identical())
    content = outcome.preconditions["content"]
    assert outcome.preconditions == {"outcome_id": "rot-1", "content": content} and len(content) == 64
    # The digest names the content: the same Snapshot content gives the same one.
    assert (
        _prepare("outcome", {"outcome": "comment"}, _identical(snapshot={"id": "hs-9"})).preconditions["content"]
        == content
    )

    hosted_b = _identical(files={**_LOCAL_FILES, "src/b.py": "fp-b-hosted"})
    hosted_extra = _identical(files={**_LOCAL_FILES, "src/c.py": "fp-c"})
    local_extra = _identical(files={"src/a.py": "fp-a"})
    target_drift = _identical()
    target_drift["targets"]["symbol:src/b.py:beta"]["fingerprint"] = "fp-beta-2"
    target_unseen = _identical()
    target_unseen["targets"]["symbol:src/c.py:gamma"] = {"path": "src/c.py", "fingerprint": "fp-gamma"}
    target_blank = _identical()
    target_blank["targets"]["file:src/a.py"] = {"path": "src/a.py"}
    for projection in (
        _projection(),  # the default hosted Snapshot differs in src/b.py and adds src/c.py
        hosted_b,
        hosted_extra,
        local_extra,
        target_drift,
        target_unseen,
        target_blank,
        _identical(truncated={"targets": True, "threads": False}),
    ):
        with pytest.raises(HostedActionRefused) as refused:
            _prepare("outcome", {"outcome": "lgtm"}, projection)
        assert refused.value.code == "content_mismatch", projection
    assert (
        _prepare("outcome", {"outcome": "lgtm"}, _identical(truncated={"threads": True})).preconditions["content"]
        == content
    )


@pytest.mark.parametrize(
    ("kind", "original", "renewed", "keeps"),
    [
        ("mark", {"mark": None, "fingerprint": "fp-a"}, {"mark": ["reviewed", "hs-1"], "fingerprint": "fp-a"}, True),
        ("mark", {"mark": None, "fingerprint": "fp-a"}, {"mark": None, "fingerprint": "fp-a2"}, False),
        ("mark", {"mark": None}, {"mark": None, "fingerprint": "fp-a"}, False),  # unbound: never carried
        ("comment", {"fingerprint": "fp-a", "anchor": "an"}, {"fingerprint": "fp-a", "anchor": "an"}, True),
        ("comment", {"fingerprint": "fp-a", "anchor": "an"}, {"fingerprint": "fp-a", "anchor": "an2"}, False),
        ("comment", {"fingerprint": "fp-a", "anchor": "an"}, {"fingerprint": "fp-a2", "anchor": "an"}, False),
        ("comment", {"fingerprint": "fp-a"}, {"fingerprint": "fp-a", "anchor": "an"}, False),
        ("outcome", {"outcome_id": "", "content": "c1"}, {"outcome_id": "rot-2", "content": "c1"}, True),
        ("outcome", {"outcome_id": "", "content": "c1"}, {"outcome_id": "", "content": "c2"}, False),
        ("outcome", {"outcome_id": ""}, {"outcome_id": "", "content": "c1"}, False),
        # Kinds with no content binding re-bind to the thread or request as it is now.
        ("reply", {"thread_version": 2}, {"thread_version": 3}, True),
        ("request_complete", {"request_version": 3}, {"request_version": 5}, True),
    ],
)
def test_a_requeue_keeps_only_actions_bound_to_unchanged_content(
    kind: str, original: dict[str, Any], renewed: dict[str, Any], keeps: bool
) -> None:
    assert requeue_keeps_content(kind, original, renewed) is keeps


def test_replies_outcomes_and_request_completion_bind_their_compare_and_set_facts() -> None:
    reply = _prepare("reply", {"parent_id": "ann-t1", "body": "agreed"})
    assert (reply.payload, reply.preconditions) == ({"parent_id": "ann-t1", "body": "agreed"}, {"thread_version": 2})
    with pytest.raises(HostedActionRefused) as unknown_thread:
        _prepare("reply", {"parent_id": "ann-gone", "body": "x"})
    assert unknown_thread.value.code == "unknown_thread"

    outcome = _prepare("outcome", {"outcome": "lgtm", "summary": "ship it"}, _identical())
    assert outcome.preconditions["outcome_id"] == "rot-1"
    assert (outcome.supersede_key, outcome.supersede) == ("outcome", True)
    first_outcome = _prepare("outcome", {"outcome": "lgtm"}, _identical(my_outcome=None))
    assert first_outcome.preconditions["outcome_id"] == ""
    assert first_outcome.payload == {"outcome": "lgtm", "summary": ""}
    # Spelled as the hosted outcome endpoint accepts it.
    assert _prepare("outcome", {"outcome": " LGTM "}, _identical()).payload["outcome"] == "lgtm"
    for body in (
        {"outcome": "approve"},
        {"outcome": 7},
        {"outcome": "lgtm", "summary": 7},
        {"outcome": "lgtm", "summary": "x" * (16 * 1024 + 1)},
    ):
        with pytest.raises(HostedActionRefused) as refused:
            _prepare("outcome", body)
        assert refused.value.code == "invalid"

    completion = _prepare("request_complete", {"request_id": "req-1"})
    assert completion.preconditions == {"request_version": 3}
    assert (completion.supersede_key, completion.supersede) == ("request:req-1", False)
    for request_id in ("req-done", "req-missing"):
        with pytest.raises(HostedActionRefused) as refused:
            _prepare("request_complete", {"request_id": request_id})
        assert refused.value.code == "unknown_request", request_id

    with pytest.raises(HostedActionRefused) as unknown_kind:
        _prepare("evidence", {})
    assert unknown_kind.value.code == "invalid"


def test_nothing_queues_against_a_closed_or_snapshotless_hosted_review() -> None:
    with pytest.raises(HostedActionRefused) as closed:
        _prepare("comment", {"path": "src/a.py", "body": "x"}, _projection(status="finished"))
    assert closed.value.code == "review_closed"
    with pytest.raises(HostedActionRefused) as snapshotless:
        _prepare("comment", {"path": "src/a.py", "body": "x"}, _projection(snapshot={}))
    assert snapshotless.value.code == "invalid_projection"


def test_a_projection_is_validated_and_bounded_before_it_is_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    parsed = parse_projection(_projection())
    assert parsed["snapshot"] == {"id": "hs-1", "number": 0}
    sparse = parse_projection({key: value for key, value in _projection().items() if key not in {"threads", "policy"}})
    assert (sparse["threads"], sparse["policy"]) == ({}, {})
    for broken in (
        [],
        _projection(principal_id=""),
        _projection(status=7),
        _projection(snapshot={"id": ""}),
        _projection(targets=["not", "an", "object"]),
    ):
        with pytest.raises(HostedActionRefused) as refused:
            parse_projection(broken)
        assert refused.value.code == "invalid_projection"
    monkeypatch.setattr(engine, "PROJECTION_TARGET_LIMIT", 3)
    monkeypatch.setattr(engine, "PROJECTION_THREAD_LIMIT", 1)
    assert parse_projection(_projection())["targets"]  # exactly at both bounds
    with pytest.raises(HostedActionRefused, match="too many targets"):
        parse_projection(_projection(files={f"f{index}": "x" for index in range(4)}))
    with pytest.raises(HostedActionRefused, match="too many targets"):
        parse_projection(_projection(targets={f"t{index}": {} for index in range(4)}))
    with pytest.raises(HostedActionRefused, match="too many threads"):
        parse_projection(_projection(threads={"a": {}, "b": {}}))


# --------------------------------------------------------------------------- #
# engine: conflict rules (evaluated by hosted)
# --------------------------------------------------------------------------- #


def _facts(**overrides: Any) -> HostedFacts:
    base: dict[str, Any] = {"review_status": "open", "snapshot_id": "hs-1"}
    base.update(overrides)
    return HostedFacts(**base)


def test_conflict_reasons_are_exact_and_ordered() -> None:
    for kind, preconditions, facts, expected in [
        ("comment", {}, _facts(), ""),
        ("comment", {}, _facts(review_status="finished"), "review_closed"),
        ("comment", {}, _facts(snapshot_id="hs-2"), "snapshot_advanced"),
        ("reply", {"thread_version": 2}, _facts(thread_version=2), ""),
        ("reply", {"thread_version": 2}, _facts(thread_version=None), "thread_deleted"),
        ("reply", {"thread_version": 2}, _facts(thread_version=3), "thread_changed"),
        ("reply", {"thread_version": 2}, _facts(thread_version=None, snapshot_id="hs-2"), "thread_deleted"),
        ("reply", {"thread_version": 2}, _facts(thread_version=3, snapshot_id="hs-2"), "thread_changed"),
        ("reply", {"thread_version": 2}, _facts(thread_version=2, snapshot_id="hs-2"), "snapshot_advanced"),
        ("reply", {"thread_version": 2}, _facts(thread_version=None, review_status="archived"), "review_closed"),
        ("request_complete", {"request_version": 3}, _facts(request_version=3), ""),
        ("request_complete", {"request_version": 3}, _facts(request_version=None), "request_changed"),
        ("request_complete", {}, _facts(request_version=None), "request_changed"),
        ("request_complete", {"request_version": 3}, _facts(request_version=4), "request_changed"),
        ("request_complete", {"request_version": 3}, _facts(request_version=4, snapshot_id="hs-2"), "request_changed"),
        (
            "request_complete",
            {"request_version": 3},
            _facts(request_version=3, snapshot_id="hs-2"),
            "snapshot_advanced",
        ),
        ("mark", {"mark": None}, _facts(), ""),
        ("mark", {"mark": ["reviewed", "hs-0"]}, _facts(mark=["reviewed", "hs-0"]), ""),
        ("mark", {"mark": None}, _facts(mark=["reviewed", "hs-1"]), "judgment_changed"),
        ("mark", {"mark": ["reviewed", "hs-0"]}, _facts(mark=["reviewed", "hs-1"]), "judgment_changed"),
        ("mark", {"mark": None}, _facts(mark=["reviewed", "hs-1"], snapshot_id="hs-2"), "snapshot_advanced"),
        ("outcome", {"outcome_id": "rot-1"}, _facts(outcome_id="rot-1"), ""),
        ("outcome", {"outcome_id": "rot-1"}, _facts(outcome_id="rot-2"), "judgment_changed"),
        ("outcome", {"outcome_id": ""}, _facts(outcome_id="rot-2", snapshot_id="hs-2"), "snapshot_advanced"),
        ("comment", {}, _facts(thread_version=None, request_version=None, mark=["x", "y"], outcome_id="o"), ""),
        # RC5: the anchor carries only onto content hosted confirms is identical.
        ("comment", {"fingerprint": "fp-a", "anchor": "an"}, _facts(target_fingerprint="fp-a", anchor="an"), ""),
        (
            "comment",
            {"fingerprint": "fp-a", "anchor": "an"},
            _facts(target_fingerprint="fp-a", anchor="an2"),
            "target_changed",
        ),
        # The old side, or a file without frozen new-side text: hosted cannot confirm the lines.
        ("comment", {"fingerprint": "fp-a", "anchor": "an"}, _facts(target_fingerprint="fp-a"), "target_changed"),
        ("comment", {"fingerprint": "fp-a", "anchor": None}, _facts(target_fingerprint="fp-a"), "target_changed"),
        # A fingerprint without its anchor names the file, not the lines.
        ("comment", {"fingerprint": "fp-a"}, _facts(target_fingerprint="fp-a", anchor="an"), "target_changed"),
        ("comment", {"fingerprint": "fp-a"}, _facts(target_fingerprint="fp-a2"), "target_changed"),
        ("comment", {"fingerprint": "fp-a"}, _facts(target_fingerprint=None), "target_changed"),
        ("comment", {"fingerprint": ""}, _facts(target_fingerprint=None), "target_changed"),
        # A judgment is of the unit, not of lines: no anchor decides it.
        ("mark", {"mark": None, "fingerprint": "fp-a"}, _facts(target_fingerprint="fp-a"), ""),
        ("mark", {"mark": None, "fingerprint": "fp-a", "anchor": "x"}, _facts(target_fingerprint="fp-a"), ""),
        ("mark", {"mark": None, "fingerprint": "fp-a"}, _facts(target_fingerprint="fp-a2"), "target_changed"),
        # Ordered: a moved Snapshot is reported first, a changed target before a changed judgment.
        ("comment", {"fingerprint": "fp-a"}, _facts(target_fingerprint="x", snapshot_id="hs-2"), "snapshot_advanced"),
        (
            "comment",
            {"fingerprint": "fp-a", "anchor": "an"},
            _facts(target_fingerprint="fp-a", anchor="x", snapshot_id="hs-2"),
            "snapshot_advanced",
        ),
        (
            "mark",
            {"mark": None, "fingerprint": "fp-a"},
            _facts(target_fingerprint="x", mark=["reviewed", "hs-1"]),
            "target_changed",
        ),
        # N-1 skew: an action queued before RC5 carries no fingerprint and is judged as before.
        ("comment", {}, _facts(target_fingerprint="anything"), ""),
        ("mark", {"mark": None}, _facts(target_fingerprint=None), ""),
        # A fingerprint never decides a kind that has no anchor content.
        ("reply", {"thread_version": 2, "fingerprint": "fp-a"}, _facts(thread_version=2), ""),
        ("outcome", {"outcome_id": "", "fingerprint": "fp-a"}, _facts(), ""),
    ]:
        assert conflict_reason(kind, "hs-1", preconditions, facts) == expected


# --------------------------------------------------------------------------- #
# engine: reconnect
# --------------------------------------------------------------------------- #


class _FakeLocal:
    def __init__(self, actions: list[dict[str, Any]], *, attachment: dict[str, Any] | None = None) -> None:
        self.actions = {row["id"]: dict(row) for row in actions}
        self.attachment = {"principal_id": "alice", "hosted_review_id": HOSTED_ID} if attachment is None else attachment
        self.recorded: list[tuple[str, str, str, str]] = []
        self.projections: list[Mapping[str, Any]] = []

    def status(self) -> Mapping[str, Any]:
        return {"attachment": self.attachment, "actions": list(self.actions.values())}

    def record_projection(self, projection: Mapping[str, Any]) -> Mapping[str, Any]:
        self.projections.append(projection)
        return {}

    def record_result(
        self, action_id: str, *, expected_state: str, state: str, reason: str, result: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        row = self.actions[action_id]
        if row["state"] != expected_state:
            raise HostedAttachmentConflict("action_state_changed", "moved")
        row.update(state=state, reason=reason, result=dict(result))
        self.recorded.append((action_id, expected_state, state, reason))
        return {}


class _FakeHosted:
    def __init__(
        self,
        answers: Mapping[str, Any],
        *,
        principal: str = "alice",
        review_id: str = HOSTED_ID,
        refreshed_principal: str | None = None,
        offline_projection: bool = False,
    ) -> None:
        self.answers = dict(answers)
        self.principal = principal
        self.review_id = review_id
        self.refreshed_principal = refreshed_principal
        self.offline_projection = offline_projection
        self.replayed: list[dict[str, Any]] = []
        self.projection_calls = 0

    def projection(self) -> Mapping[str, Any]:
        self.projection_calls += 1
        if self.offline_projection:
            raise HostedUnavailable("down")
        principal = self.principal
        if self.projection_calls > 1 and self.refreshed_principal is not None:
            principal = self.refreshed_principal
        return {"principal_id": principal, "review_id": self.review_id, "snapshot": {"id": "hs-2"}}

    def replay(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.replayed.append(dict(request))
        answer = self.answers[str(request["action_id"])]
        if isinstance(answer, Exception):
            raise answer
        return {"action_id": request["action_id"], **answer}


def _queued(action_id: str, sequence: int, state: str = "pending", kind: str = "comment") -> dict[str, Any]:
    return {
        "id": action_id,
        "sequence": sequence,
        "kind": kind,
        "snapshot_id": "hs-1",
        "payload": {"body": action_id},
        "preconditions": {},
        "state": state,
    }


def test_reconnect_replays_pending_actions_in_order_and_records_each_exact_answer() -> None:
    local = _FakeLocal(
        [
            _queued("a3", 3),
            _queued("a1", 1),
            _queued("a2", 2, kind="reply"),
            _queued("done", 0, state="applied"),
            _queued("stuck", 4, state="conflict"),
            _queued("gone", 5, state="discarded"),
        ]
    )
    hosted = _FakeHosted(
        {
            "a1": {"state": "applied", "reason": ""},
            "a2": {"state": "conflict", "reason": "thread_deleted"},
            "a3": {"state": "conflict", "reason": "snapshot_advanced"},
        }
    )
    report = reconnect(local, hosted)
    assert [row["action_id"] for row in hosted.replayed] == ["a1", "a2", "a3"]
    assert hosted.replayed[0] == {
        "action_id": "a1",
        "kind": "comment",
        "snapshot_id": "hs-1",
        "payload": {"body": "a1"},
        "preconditions": {},
        "actor_id": "alice",
    }
    assert local.recorded == [
        ("a1", "pending", "applied", ""),
        ("a2", "pending", "conflict", "thread_deleted"),
        ("a3", "pending", "conflict", "snapshot_advanced"),
    ]
    assert [(row["action_id"], row["state"], row["reason"]) for row in report.results] == [
        ("a1", "applied", ""),
        ("a2", "conflict", "thread_deleted"),
        ("a3", "conflict", "snapshot_advanced"),
    ]
    assert (report.identity, report.stopped, report.remaining, report.projection_refreshed) == ("ok", "", 0, True)
    assert local.projections and local.projections[-1]["snapshot"] == {"id": "hs-2"}
    assert report.to_wire()["results"][0]["overrode"] == ""


def test_reconnect_while_offline_replays_nothing_and_keeps_everything_pending() -> None:
    local = _FakeLocal([_queued("a1", 1), _queued("a2", 2)])
    report = reconnect(local, _FakeHosted({}, offline_projection=True))
    assert (report.stopped, report.remaining, report.results, local.recorded) == ("offline", 2, [], [])
    assert local.projections == []


def test_reconnect_stops_where_hosted_drops_and_leaves_the_rest_pending() -> None:
    local = _FakeLocal([_queued("a1", 1), _queued("a2", 2), _queued("a3", 3)])
    hosted = _FakeHosted({"a1": {"state": "applied", "reason": ""}, "a2": HostedUnavailable("dropped")})
    report = reconnect(local, hosted)
    assert (report.stopped, report.remaining) == ("offline", 2)
    assert local.recorded == [("a1", "pending", "applied", "")]
    assert [row["action_id"] for row in hosted.replayed] == ["a1", "a2"]
    assert local.actions["a3"]["state"] == "pending"


@pytest.mark.parametrize(
    ("hosted", "identity"),
    [
        (_FakeHosted({}, principal="mallory"), "mismatch"),
        (_FakeHosted({}, review_id="another-hosted-review"), "mismatch"),
    ],
)
def test_reconnect_as_another_principal_or_review_replays_nothing(hosted: _FakeHosted, identity: str) -> None:
    local = _FakeLocal([_queued("a1", 1)])
    report = reconnect(local, hosted)
    assert (report.identity, hosted.replayed, local.recorded, local.projections) == (identity, [], [], [])


def test_reconnect_of_an_unattached_review_does_nothing() -> None:
    local = _FakeLocal([_queued("a1", 1)], attachment={})
    hosted = _FakeHosted({})
    assert reconnect(local, hosted).identity == "not_attached"
    assert hosted.projection_calls == 0


def test_a_refused_credential_stops_but_a_rejected_payload_ends_only_that_action() -> None:
    local = _FakeLocal([_queued("a1", 1), _queued("a2", 2), _queued("a3", 3)])
    hosted = _FakeHosted(
        {
            "a1": HostedReplayRefused("rejected", "line 99 is past the end"),
            "a2": HostedReplayRefused("forbidden", "no judge permission"),
        }
    )
    report = reconnect(local, hosted)
    assert local.recorded == [("a1", "pending", "conflict", "rejected")]
    assert local.actions["a1"]["result"]["message"] == "line 99 is past the end"
    assert (report.stopped, report.remaining) == ("forbidden", 2)
    assert [row["action_id"] for row in hosted.replayed] == ["a1", "a2"]


def test_a_refused_projection_stops_before_any_replay() -> None:
    class _Refusing(_FakeHosted):
        def projection(self) -> Mapping[str, Any]:
            raise HostedReplayRefused("unauthenticated", "expired")

    hosted = _Refusing({})
    report = reconnect(_FakeLocal([_queued("a1", 1)]), hosted)
    assert (report.stopped, report.remaining, hosted.replayed) == ("unauthenticated", 1, [])


def test_an_unrecognised_hosted_answer_is_never_recorded() -> None:
    for answer in [
        {"state": "merged", "reason": ""},
        {"state": "conflict", "reason": "because"},
        {"state": "conflict"},
        {"state": "applied", "reason": "", "action_id": "someone-else"},
        {"state": "discarded", "reason": "snapshot_advanced"},
    ]:
        local = _FakeLocal([_queued("a1", 1), _queued("a2", 2)])

        class _Odd(_FakeHosted):

            def replay(self, request: Mapping[str, Any], _answer: Mapping[str, Any] = answer) -> Mapping[str, Any]:
                self.replayed.append(dict(request))
                return {"action_id": request["action_id"], **_answer}

        report = reconnect(local, _Odd({}))
        assert (report.stopped, report.remaining, local.recorded) == ("invalid_response", 2, [])


def test_an_action_discarded_in_flight_records_what_hosted_did_over_the_discard() -> None:
    local = _FakeLocal([_queued("a1", 1)])

    class _DiscardWhileReplaying(_FakeHosted):
        def replay(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
            local.actions["a1"]["state"] = "discarded"  # the reviewer discarded it meanwhile
            return super().replay(request)

    report = reconnect(local, _DiscardWhileReplaying({"a1": {"state": "applied", "reason": ""}}))
    assert local.recorded == [("a1", "discarded", "applied", "")]
    assert local.actions["a1"]["result"]["overrode"] == "discarded"
    assert report.results[0]["overrode"] == "discarded"


def test_an_action_gone_from_the_local_queue_in_flight_stops_the_replay() -> None:
    local = _FakeLocal([_queued("a1", 1), _queued("a2", 2)])

    class _PrunedWhileReplaying(_FakeHosted):
        def replay(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
            local.actions[str(request["action_id"])]["state"] = "applied"  # moved by another sync
            return super().replay(request)

    hosted = _PrunedWhileReplaying({"a1": {"state": "applied"}, "a2": {"state": "applied"}})
    report = reconnect(local, hosted)
    assert (report.stopped, report.remaining, report.results, local.recorded) == ("local_changed", 2, [], [])
    assert [row["action_id"] for row in hosted.replayed] == ["a1"]


@pytest.mark.parametrize(("principal", "review_id"), [("mallory", HOSTED_ID), ("alice", "another-hosted-review")])
def test_a_refreshed_projection_for_another_principal_or_review_is_not_stored(principal: str, review_id: str) -> None:
    local = _FakeLocal([_queued("a1", 1)])

    class _Swapped(_FakeHosted):
        def projection(self) -> Mapping[str, Any]:
            current = dict(super().projection())
            if self.projection_calls > 1:
                current.update(principal_id=principal, review_id=review_id)
            return current

    report = reconnect(local, _Swapped({"a1": {"state": "applied"}}))
    assert local.recorded == [("a1", "pending", "applied", "")]
    assert report.projection_refreshed is False
    assert local.projections == []


@pytest.mark.parametrize("failure", [HostedUnavailable("dropped"), HostedReplayRefused("forbidden", "revoked")])
def test_a_refresh_that_fails_after_replay_keeps_the_recorded_answers(failure: Exception) -> None:
    local = _FakeLocal([_queued("a1", 1)])

    class _DropsAfter(_FakeHosted):
        def projection(self) -> Mapping[str, Any]:
            if self.projection_calls:
                raise failure
            return super().projection()

    report = reconnect(local, _DropsAfter({"a1": {"state": "applied"}}))
    assert local.recorded == [("a1", "pending", "applied", "")]
    assert report.projection_refreshed is False


# --------------------------------------------------------------------------- #
# local Reader API
# --------------------------------------------------------------------------- #


class _Reader:
    def __init__(self, client: TestClient, store: ReviewStore, session: ReviewSession, revision: ReviewRevision):
        self.client = client
        self.store = store
        self.session = session
        self.revision = revision
        self.base = f"/api/reviews/{session.id}"

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        return self.client.request(method, path, headers={"Authorization": f"Bearer {TOKEN}"}, **kwargs)

    def attach(self, **overrides: Any) -> Any:
        body = {
            "origin": ORIGIN,
            "hosted_repo_id": "hosted-repo",
            "hosted_review_id": HOSTED_ID,
            "principal_id": "alice",
            "projection": _projection(),
        }
        body.update(overrides)
        return self.call("PUT", f"{self.base}/hosted", json=body)

    def queue(self, body: Mapping[str, Any]) -> Any:
        return self.call("POST", f"{self.base}/hosted/actions", json=dict(body))

    def op(self, action_id: str, body: Mapping[str, Any]) -> Any:
        return self.call("POST", f"{self.base}/hosted/actions/{action_id}", json=dict(body))


@pytest.fixture
def reader(store: ReviewStore, repo_root: Path) -> _Reader:
    session, revision = _seed(store, repo_root)
    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency(TOKEN), repo_root=repo_root, port=PORT)
    return _Reader(TestClient(app, base_url=BASE), store, session, revision)


def test_attach_validates_identity_projection_and_shared_content(reader: _Reader) -> None:
    assert reader.call("GET", f"{reader.base}/hosted").json() == {"attached": False, "review_id": reader.session.id}
    refusals = [
        ({"origin": "https://hosted.example/path"}, 422, "origin"),
        ({"origin": "ftp://hosted.example"}, 422, "origin"),
        ({"origin": "https://user:pw@hosted.example"}, 422, "origin"),
        ({"origin": "https://user@hosted.example"}, 422, "origin"),
        ({"origin": "https://:pw@hosted.example"}, 422, "origin"),
        ({"origin": "https://hosted.example#frag"}, 422, "origin"),
        ({"origin": "https://:443"}, 422, "origin"),
        ({"origin": "https://hosted.example?x=1"}, 422, "origin"),
        ({"principal_id": ""}, 422, "required"),
        ({"projection": {"review_id": HOSTED_ID}}, 422, "invalid_projection"),
        ({"principal_id": "bob"}, 422, "projection_mismatch"),
        ({"hosted_review_id": "another-hosted"}, 422, "projection_mismatch"),
        ({"hosted_repo_id": "another-repo"}, 422, "projection_mismatch"),
        (
            {"projection": _projection(targets={"file:src/a.py": {"fingerprint": "other"}}, files={})},
            409,
            "no_shared_content",
        ),
        # A symbol unit's fingerprint is never a file's.
        ({"projection": _projection(targets={}, files={"src/b.py": "fp-beta"})}, 409, "no_shared_content"),
    ]
    for overrides, status, detail in refusals:
        response = reader.attach(**overrides)
        assert response.status_code == status, (overrides, response.text)
        assert detail in response.json()["detail"], overrides
    assert reader.store.get_hosted_attachment(reader.session.id) is None

    # One shared file is enough (no target matches here).
    files_only = reader.attach(
        projection=_projection(targets={}, files={"src/a.py": "fp-a"}), origin="HTTPS://Hosted.Example/"
    )
    assert files_only.status_code == 200, files_only.text
    payload = files_only.json()
    assert payload["created"] is True and payload["attached"] is True
    assert payload["reference"] == f"r/{HOSTED_ID}"
    assert payload["attachment"]["origin"] == "https://hosted.example"
    assert payload["attachment"]["url"] == f"https://hosted.example/r/{HOSTED_ID}"
    assert payload["owns"] == ["comments", "judgments", "requests", "outcomes", "policy"]
    assert payload["limits"]["unresolved"] == engine.UNRESOLVED_ACTION_LIMIT

    # A refresh of the same attachment is not re-checked for shared content.
    refresh = reader.attach(projection=_projection(targets={}, files={}))
    assert refresh.status_code == 200 and refresh.json()["created"] is False
    retarget = reader.attach(hosted_review_id="other", projection=_projection(review_id="other"))
    assert retarget.status_code == 409 and retarget.json()["detail"].startswith("already_attached")


def test_an_attached_review_shows_one_identity_everywhere(reader: _Reader) -> None:
    before = reader.call("GET", "/api/reviews", params={"status_filter": "all"}).json()["reviews"]
    assert before[0]["ref"] != f"r/{HOSTED_ID}" and "hosted" not in before[0]
    assert reader.attach().status_code == 200

    listed = reader.call("GET", "/api/reviews", params={"status_filter": "all"}).json()["reviews"]
    # The canonical Change is hosted's (RC5): even ``id`` names it; the local id is provenance only.
    assert [item for item in listed if item["id"] == reader.session.id] == []
    row = next(item for item in listed if item["id"] == HOSTED_ID)
    assert (row["ref"], row["change_ref"], row["short_id"]) == (f"r/{HOSTED_ID}", f"r/{HOSTED_ID}", HOSTED_ID)
    assert row["hosted"]["reference"] == f"r/{HOSTED_ID}"
    assert row["hosted"]["url"] == f"{ORIGIN}/r/{HOSTED_ID}"
    assert row["hosted"]["local_review_id"] == reader.session.id
    located = reader.call("GET", f"/api/revisions/{reader.revision.id}").json()["review"]
    assert (located["id"], located["ref"], located["short_id"]) == (HOSTED_ID, f"r/{HOSTED_ID}", HOSTED_ID)
    # The hosted id routes back to this local Review, and so does the local id.
    by_hosted = reader.call("GET", f"/api/reviews/{HOSTED_ID}/hosted")
    assert by_hosted.status_code == 200 and by_hosted.json()["review_id"] == reader.session.id
    assert reader.call("GET", f"/api/reviews/{HOSTED_ID}").json()["session"]["id"] == HOSTED_ID
    assert reader.call("GET", reader.base).json()["session"]["id"] == HOSTED_ID
    without_provenance = json.dumps({**row, "hosted": {**row["hosted"], "local_review_id": ""}})
    assert reader.session.id not in without_provenance
    assert reader.store.short_session_reference(reader.session.id) not in without_provenance

    reader.call("DELETE", f"{reader.base}/hosted")
    detached = reader.call("GET", f"/api/revisions/{reader.revision.id}").json()["review"]
    assert detached["id"] == reader.session.id
    assert detached["ref"] == f"r/{reader.store.short_session_reference(reader.session.id)}"
    assert "hosted" not in detached


def test_a_hosted_id_attached_on_two_origins_fails_closed_on_the_reader(reader: _Reader) -> None:
    assert reader.attach().status_code == 200
    other, _revision = _seed(reader.store, Path(reader.session.repo_root))
    _attach_bypassing_the_store(reader.store, other.id, "https://other.example")
    ambiguous = reader.call("POST", f"/api/reviews/{HOSTED_ID}/marks", json={"unit_key": "file:src/a.py"})
    assert ambiguous.status_code == 409 and "ambiguous" in ambiguous.json()["detail"]
    assert reader.store.list_hosted_actions(reader.session.id) == ()
    assert reader.store.list_hosted_actions(other.id) == ()


#: Online-only while attached (RC5): everything hosted owns that the Reader does not queue.
_HOSTED_OWNED_WRITES: list[tuple[str, str, dict[str, Any]]] = [
    ("POST", "marks/bulk", {"unit_keys": ["file:src/a.py"]}),
    ("POST", "marks/bulk/undo", {}),
    ("POST", "behavior/marks", {}),
    ("POST", "decisions/retention/events", {}),
    ("POST", "finish", {"status": "finished"}),
    ("POST", "landing/arm", {}),
    ("POST", "verification/waivers", {}),
    ("POST", "github/publish", {}),
    # Provider operations (RC5): never queued, and never run locally while hosted owns them.
    ("POST", "provider-review/dismiss", {"remote_ref": "github-review:7"}),
    ("POST", "post-merge/notification", {"confirm": True}),
    # P3-2: self-reported seen coverage is a Review fact hosted owns while attached.
    ("POST", "seen-coverage", {"action": "skipped", "block_keys": []}),
]


@pytest.mark.parametrize(("method", "suffix", "body"), _HOSTED_OWNED_WRITES)
def test_hosted_owned_writes_are_refused_while_attached_and_only_then(
    reader: _Reader, method: str, suffix: str, body: dict[str, Any]
) -> None:
    path = f"{reader.base}/{suffix}"
    payload = {**body, "expected_revision_id": reader.revision.id}
    before = reader.call(method, path, json=payload)
    assert not str(before.json().get("detail", "")).startswith("hosted_owned"), before.text

    assert reader.attach().status_code == 200
    refused = reader.call(method, path, json=payload)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"].startswith(f"hosted_owned: this Review is attached to hosted r/{HOSTED_ID}")
    assert "online-only" in refused.json()["detail"]
    assert reader.store.list_hosted_actions(reader.session.id) == ()


# --------------------------------------------------------------------------- #
# local Reader API: queue-safe writes while attached (RC5)
# --------------------------------------------------------------------------- #


def _local_state(reader: _Reader) -> tuple[Any, ...]:
    """Everything a queued Reader write must leave untouched in the local Review."""

    return (
        [(mark.unit_key, mark.state) for mark in reader.store.list_marks(reader.session.id)],
        [(row.id, row.body) for row in reader.store.list_annotations(reader.session.id)],
        reader.store.latest_outcome(reader.session.id, reader.session.reviewer_id),
    )


def _write(reader: _Reader, suffix: str, body: Mapping[str, Any], *, review: str = "") -> Any:
    payload = {**body, "expected_revision_id": reader.revision.id}
    return reader.call("POST", f"/api/reviews/{review or reader.session.id}/{suffix}", json=payload)


def test_a_judgment_queues_while_attached_and_applies_locally_only_when_not(reader: _Reader) -> None:
    reader.store.set_current_revision(reader.session.id, reader.revision.id)
    local = _write(reader, "marks", {"unit_key": "file:src/a.py", "state": "reviewed"})
    assert local.status_code == 200, local.text
    assert reader.store.list_marks(reader.session.id)
    before = _local_state(reader)

    assert reader.attach().status_code == 200
    # Routed by the hosted id the attached Reader shows.
    queued = _write(reader, "marks", {"unit_key": "file:src/a.py", "state": "needs_changes"}, review=HOSTED_ID)
    assert queued.status_code == 202, queued.text
    payload = queued.json()
    assert (payload["queued"], payload["reference"], payload["superseded"]) == (True, f"r/{HOSTED_ID}", [])
    [action] = payload["actions"]
    assert (action["kind"], action["state"], action["snapshot_id"]) == ("mark", "pending", "hs-1")
    assert action["payload"] == {"unit_key": "file:src/a.py", "state": "needs_changes", "fingerprint": "fp-a"}
    assert action["preconditions"] == {"mark": ["needs_changes", "hs-0"], "fingerprint": "fp-a"}
    assert payload["counts"]["pending"] == 1
    assert _local_state(reader) == before  # hosted owns judgments: nothing is applied locally

    # A newer judgment on the same unit supersedes the queued one.
    again = _write(reader, "marks", {"unit_key": "file:src/a.py", "state": "reviewed"})
    assert again.status_code == 202 and again.json()["superseded"] == [action["id"]]


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        # A path spelling would widen locally; a queued judgment names its exact unit.
        ({"path": "src/a.py"}, 422, "invalid"),
        ({"unit_key": "file:src/a.py", "state": "approved"}, 422, "invalid"),
        ({"unit_key": "file:src/b.py"}, 409, "unknown_target"),  # local only
        ({"unit_key": "file:src/c.py"}, 409, "unknown_target"),  # hosted only: never seen
        ({}, 422, "invalid"),
    ],
)
def test_a_judgment_that_cannot_queue_is_refused_and_never_applied(
    reader: _Reader, body: dict[str, Any], status: int, detail: str
) -> None:
    assert reader.attach().status_code == 200
    before = _local_state(reader)
    refused = _write(reader, "marks", body)
    assert refused.status_code == status, refused.text
    assert refused.json()["detail"].startswith(detail)
    assert reader.store.list_hosted_actions(reader.session.id) == ()
    assert _local_state(reader) == before


def test_a_judgment_on_drifted_content_is_refused_not_guessed(reader: _Reader) -> None:
    drifted = _projection()
    drifted["targets"]["file:src/a.py"]["fingerprint"] = "fp-a-hosted"
    assert reader.attach(projection=drifted).status_code == 200
    refused = _write(reader, "marks", {"unit_key": "file:src/a.py"})
    assert refused.status_code == 409 and refused.json()["detail"].startswith("content_mismatch")
    assert reader.store.list_hosted_actions(reader.session.id) == ()


def test_a_stale_reader_write_is_refused_before_it_queues(reader: _Reader) -> None:
    assert reader.attach().status_code == 200
    for suffix, body in (
        ("marks", {"unit_key": "file:src/a.py"}),
        ("annotations", {"path": "src/a.py", "body": "x", "file_level": True}),
        ("outcome", {"outcome": "lgtm"}),
    ):
        stale = reader.call("POST", f"{reader.base}/{suffix}", json={**body, "expected_revision_id": "rev-old"})
        assert stale.status_code == 409, (suffix, stale.text)
    assert reader.store.list_hosted_actions(reader.session.id) == ()


def test_comments_and_replies_queue_with_the_readers_own_spellings(reader: _Reader) -> None:
    assert reader.attach().status_code == 200
    before = _local_state(reader)
    nested = _write(
        reader,
        "annotations",
        {"path": "src/a.py", "body": "why?", "kind": "suggestion", "range": {"start": 3, "end": 5, "side": "new"}},
    )
    assert nested.status_code == 202, nested.text
    [comment] = nested.json()["actions"]
    assert comment["kind"] == "comment"
    assert comment["payload"] == {
        "path": "src/a.py",
        "body": "why?",
        "kind": "suggestion",
        "side": "new",
        "file_level": False,
        "start_line": 3,
        "end_line": 5,
    }
    assert comment["preconditions"] == {"fingerprint": "fp-a", "anchor": _ANCHOR_A}
    one_line = _write(reader, "annotations", {"path": "src/a.py", "body": "x", "start_line": 4, "end_line": 0})
    assert one_line.status_code == 202, one_line.text
    assert (
        one_line.json()["actions"][0]["payload"]["start_line"],
        one_line.json()["actions"][0]["payload"]["end_line"],
    ) == (4, 4)
    file_level = _write(reader, "annotations", {"path": "src/a.py", "body": "x", "file_level": True, "start": 9})
    assert file_level.json()["actions"][0]["payload"]["start_line"] == 0
    # The diff viewer names sides additions/deletions; hosted anchors on new/old.
    spelled = _write(reader, "annotations", {"path": "src/a.py", "body": "x", "start_line": 2, "side": "additions"})
    assert spelled.status_code == 202, spelled.text
    assert spelled.json()["actions"][0]["payload"]["side"] == "new"
    # Old-side text is kept nowhere, so a comment on a deletion is never queued unconfirmable.
    for old in ({"side": "deletions"}, {"range": {"start": 2, "side": "deletions"}}, {"side": "old"}):
        deletion = _write(reader, "annotations", {"path": "src/a.py", "body": "x", "start_line": 2, **old})
        assert deletion.status_code == 409 and deletion.json()["detail"].startswith("online_only"), deletion.text

    reply = _write(reader, "annotations", {"path": "src/a.py", "body": "agreed", "parent_id": "ann-t1"})
    assert reply.status_code == 202, reply.text
    [queued_reply] = reply.json()["actions"]
    assert (queued_reply["kind"], queued_reply["payload"], queued_reply["preconditions"]) == (
        "reply",
        {"parent_id": "ann-t1", "body": "agreed"},
        {"thread_version": 2},
    )
    assert _local_state(reader) == before
    assert len(reader.store.list_hosted_actions(reader.session.id)) == 5


def test_a_comment_without_exact_new_side_text_is_online_only(reader: _Reader) -> None:
    # src/a.py is identical by fingerprint, but this revision has no frozen text for it
    # (a deleted or binary file, or one never captured): its lines cannot be confirmed.
    reader.store.write_blob_artifact(reader.session.id, reader.revision.id, {"src/b.py": _LOCAL_TEXT["src/b.py"]})
    assert reader.attach().status_code == 200
    before = _local_state(reader)
    for body in ({"start_line": 2}, {"file_level": True}):
        refused = _write(reader, "annotations", {"path": "src/a.py", "body": "x", **body})
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"].startswith("online_only") and "hosted Review" in refused.json()["detail"]
    assert reader.store.list_hosted_actions(reader.session.id) == ()
    assert _local_state(reader) == before


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        ({"path": "src/a.py", "body": "x"}, 422, "invalid"),  # a line comment names its line
        ({"path": "src/a.py", "body": "x", "start_line": "2"}, 422, "invalid"),  # never cast
        ({"path": "src/a.py", "body": " ", "file_level": True}, 422, "invalid"),
        ({"path": "src/b.py", "body": "x", "file_level": True}, 409, "content_mismatch"),
        ({"path": "src/c.py", "body": "x", "file_level": True}, 409, "unknown_target"),
        ({"path": "src/a.py", "body": "x", "parent_id": "ann-gone"}, 409, "unknown_thread"),
        ({"path": "src/a.py", "body": "x", "parent_id": "ann-t1", "mark_target": True}, 422, "invalid"),
        ({"path": "src/a.py", "body": "x", "file_level": True, "mark_target": True}, 422, "invalid"),
        (
            {"path": "src/a.py", "body": "x", "file_level": True, "kind": "request_change", "mark_target": True},
            422,
            "invalid",
        ),
    ],
)
def test_a_comment_that_cannot_queue_is_refused_and_never_applied(
    reader: _Reader, body: dict[str, Any], status: int, detail: str
) -> None:
    assert reader.attach().status_code == 200
    before = _local_state(reader)
    refused = _write(reader, "annotations", body)
    assert refused.status_code == status, refused.text
    assert refused.json()["detail"].startswith(detail)
    assert reader.store.list_hosted_actions(reader.session.id) == ()
    assert _local_state(reader) == before


def test_a_request_change_that_marks_its_target_queues_both_or_neither(
    reader: _Reader, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert reader.attach().status_code == 200
    body = {
        "path": "src/a.py",
        "body": "fix this",
        "kind": "request_change",
        "start_line": 2,
        "mark_target": True,
        "target_unit_key": "file:src/a.py",
    }
    # Bound at one unresolved action: the comment would fit, the mark would not.
    monkeypatch.setattr(review_api, "UNRESOLVED_ACTION_LIMIT", 1)
    full = _write(reader, "annotations", body)
    assert full.status_code == 409 and full.json()["detail"].startswith("queue_full")
    assert reader.store.list_hosted_actions(reader.session.id) == ()
    # A target that cannot queue refuses the comment with it.
    monkeypatch.setattr(review_api, "UNRESOLVED_ACTION_LIMIT", engine.UNRESOLVED_ACTION_LIMIT)
    unknown = _write(reader, "annotations", {**body, "target_unit_key": "symbol:src/a.py:gone"})
    assert unknown.status_code == 409 and unknown.json()["detail"].startswith("unknown_target")
    assert reader.store.list_hosted_actions(reader.session.id) == ()

    both = _write(reader, "annotations", body)
    assert both.status_code == 202, both.text
    assert [(row["kind"], row["sequence"]) for row in both.json()["actions"]] == [("comment", 1), ("mark", 2)]
    assert both.json()["actions"][1]["payload"]["state"] == "needs_changes"


def test_a_request_change_never_marks_a_target_in_another_file(reader: _Reader) -> None:
    # Both files' content is what hosted shows: only the pairing is wrong.
    assert reader.attach(projection=_identical()).status_code == 200
    body = {
        "path": "src/a.py",
        "body": "fix this",
        "kind": "request_change",
        "start_line": 2,
        "mark_target": True,
        "target_unit_key": "symbol:src/b.py:beta",
    }
    refused = _write(reader, "annotations", body)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == "invalid: annotation target belongs to a different file"
    assert reader.store.list_hosted_actions(reader.session.id) == ()
    # The same target, commented on in its own file, queues both.
    own = _write(reader, "annotations", {**body, "path": "src/b.py", "start_line": 1})
    assert own.status_code == 202, own.text
    assert [row["kind"] for row in own.json()["actions"]] == ["comment", "mark"]


def test_an_outcome_queues_while_attached(reader: _Reader) -> None:
    assert reader.attach(projection=_identical()).status_code == 200
    before = _local_state(reader)
    queued = _write(reader, "outcome", {"outcome": " LGTM ", "summary": "ship it"})
    assert queued.status_code == 202, queued.text
    [action] = queued.json()["actions"]
    assert (action["kind"], action["payload"], action["preconditions"]["outcome_id"]) == (
        "outcome",
        {"outcome": "lgtm", "summary": "ship it"},
        "rot-1",
    )
    assert set(action["preconditions"]) == {"outcome_id", "content"}
    refused = _write(reader, "outcome", {"outcome": "approve"})
    assert refused.status_code == 422 and refused.json()["detail"].startswith("invalid")
    closed = _identical(status="finished")
    assert reader.attach(projection=closed).status_code == 200
    after_close = _write(reader, "outcome", {"outcome": "lgtm"})
    assert after_close.status_code == 409 and after_close.json()["detail"].startswith("review_closed")
    assert _local_state(reader) == before


def _refresh_local(
    reader: _Reader,
    number: int,
    fingerprints: Mapping[str, str],
    text: Mapping[str, str],
    *,
    capture_kind: str = "legacy",
) -> ReviewRevision:
    """The reviewer refreshes the local Review: a new current revision of *fingerprints* by path."""

    revision = reader.store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=reader.session.id,
            revision_number=number,
            range_mode="working_tree",
            tree_fingerprint=f"tree-{number}",
            packet_schema_version=1,
            capture_kind=capture_kind,  # type: ignore[arg-type]
        ),
        [
            ReviewUnit(revision_id="", unit_key=f"file:{path}", kind="file", path=path, content_fingerprint=fp)
            for path, fp in fingerprints.items()
        ],
    )
    reader.store.write_blob_artifact(reader.session.id, revision.id, text)
    reader.store.set_current_revision(reader.session.id, revision.id)
    reader.revision = revision
    return revision


_S2_A = {"src/a.py": "alpha = 2\n"}


def _s2(**overrides: Any) -> dict[str, Any]:
    """Hosted N+1, where src/a.py changed: exactly what a refreshed local Review shows."""

    return _projection(
        snapshot={"id": "hs-2", "number": 1},
        files={"src/a.py": "fp-a2"},
        targets={"file:src/a.py": {"path": "src/a.py", "state": "unreviewed", "fingerprint": "fp-a2"}},
        my_marks={},
        **overrides,
    )


def test_a_verdict_on_content_hosted_does_not_have_is_refused_not_queued(reader: _Reader) -> None:
    assert reader.attach(projection=_identical()).status_code == 200
    # The local Review moves to content hosted's Snapshot does not have at all.
    _refresh_local(reader, 2, {"src/z.py": "fp-z"}, {"src/z.py": "zed\n"})
    before = _local_state(reader)
    unrelated = _write(reader, "outcome", {"outcome": "lgtm"})
    assert unrelated.status_code == 409 and unrelated.json()["detail"].startswith("content_mismatch"), unrelated.text
    # The same content as hosted, but a running draft: a verdict needs a saved Snapshot.
    _refresh_local(reader, 3, dict(_LOCAL_FILES), _LOCAL_TEXT, capture_kind="draft")
    draft = _write(reader, "outcome", {"outcome": "lgtm"})
    assert draft.status_code == 409 and "Running drafts" in draft.json()["detail"], draft.text
    explicit = reader.queue({"kind": "outcome", "outcome": "lgtm"})
    assert explicit.status_code == 409 and "Running drafts" in explicit.json()["detail"], explicit.text
    assert reader.store.list_hosted_actions(reader.session.id) == ()
    assert _local_state(reader) == before


def test_requeue_after_the_local_review_is_refreshed_never_carries_a_judgment_or_anchor(reader: _Reader) -> None:
    reader.store.set_current_revision(reader.session.id, reader.revision.id)
    assert reader.attach().status_code == 200
    comment = _write(reader, "annotations", {"path": "src/a.py", "body": "line 2 is wrong", "start_line": 2})
    mark = _write(reader, "marks", {"unit_key": "file:src/a.py", "state": "reviewed"})
    assert comment.status_code == mark.status_code == 202, (comment.text, mark.text)
    ids = [comment.json()["actions"][0]["id"], mark.json()["actions"][0]["id"]]
    for action_id in ids:
        reader.op(action_id, {"op": "result", "state": "conflict", "reason": "snapshot_advanced", "result": {}})

    # Sync refreshed the projection to S2 and the reviewer refreshed the local Review to
    # the same new content: a fresh action would queue, but the old ones judged S1's text.
    _refresh_local(reader, 2, {"src/a.py": "fp-a2"}, _S2_A)
    assert reader.attach(projection=_s2()).status_code == 200
    for action_id in ids:
        refused = reader.op(action_id, {"op": "requeue"})
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"].startswith("content_changed")
        kept = reader.store.get_hosted_action(reader.session.id, action_id)
        assert kept is not None and (kept.state, kept.reason) == ("conflict", "snapshot_advanced")
    assert len(reader.store.list_hosted_actions(reader.session.id)) == 2
    # The explicit fresh action on what is on screen is the way forward.
    fresh = _write(reader, "marks", {"unit_key": "file:src/a.py", "state": "reviewed"})
    assert fresh.status_code == 202 and fresh.json()["actions"][0]["preconditions"]["fingerprint"] == "fp-a2"


def test_requeue_refuses_a_comment_whose_lines_moved_under_an_equal_fingerprint(reader: _Reader) -> None:
    assert reader.attach().status_code == 200
    queued = _write(reader, "annotations", {"path": "src/a.py", "body": "line 2", "start_line": 2})
    action_id = queued.json()["actions"][0]["id"]
    reader.op(action_id, {"op": "result", "state": "conflict", "reason": "target_changed", "result": {}})
    # Same normalized fingerprint (a blank first line), but line 2 is another line now.
    _refresh_local(reader, 2, dict(_LOCAL_FILES), {**_LOCAL_TEXT, "src/a.py": "\n" + _LOCAL_TEXT["src/a.py"]})
    refused = reader.op(action_id, {"op": "requeue"})
    assert refused.status_code == 409 and refused.json()["detail"].startswith("content_changed"), refused.text
    # Back on the exact text the comment was made on, the explicit requeue binds again.
    _refresh_local(reader, 3, dict(_LOCAL_FILES), _LOCAL_TEXT)
    assert reader.op(action_id, {"op": "requeue"}).status_code == 200


def test_requeue_of_a_verdict_carries_it_only_onto_the_same_content(reader: _Reader) -> None:
    assert reader.attach(projection=_identical()).status_code == 200
    first = _write(reader, "outcome", {"outcome": "lgtm"}).json()["actions"][0]["id"]
    second = _write(reader, "outcome", {"outcome": "comment"}).json()["actions"][0]["id"]
    assert reader.store.get_hosted_action(reader.session.id, first).state == "discarded"  # type: ignore[union-attr]
    reader.op(second, {"op": "result", "state": "conflict", "reason": "judgment_changed", "result": {}})
    # Another device recorded a verdict on the same Snapshot: the reviewer re-applies theirs.
    assert reader.attach(projection=_identical(my_outcome={"id": "rot-9"})).status_code == 200
    requeued = reader.op(second, {"op": "requeue"})
    assert requeued.status_code == 200, requeued.text
    third = requeued.json()["action"]
    assert third["preconditions"]["outcome_id"] == "rot-9"
    reader.op(third["id"], {"op": "result", "state": "conflict", "reason": "snapshot_advanced", "result": {}})
    # Hosted N+1 changed src/a.py and the local Review followed: the verdict covered S1.
    _refresh_local(reader, 2, {"src/a.py": "fp-a2", "src/b.py": "fp-b"}, {**_LOCAL_TEXT, **_S2_A})
    s2 = _identical(snapshot={"id": "hs-2", "number": 1}, files={"src/a.py": "fp-a2", "src/b.py": "fp-b"})
    s2["targets"] = {"file:src/a.py": {"path": "src/a.py", "fingerprint": "fp-a2"}}
    assert reader.attach(projection=s2).status_code == 200
    refused = reader.op(third["id"], {"op": "requeue"})
    assert refused.status_code == 409 and refused.json()["detail"].startswith("content_changed"), refused.text
    assert _write(reader, "outcome", {"outcome": "lgtm"}).status_code == 202  # a fresh verdict queues


def test_a_reader_write_after_detach_applies_locally_again(reader: _Reader) -> None:
    reader.store.set_current_revision(reader.session.id, reader.revision.id)
    assert reader.attach().status_code == 200
    assert _write(reader, "marks", {"unit_key": "file:src/a.py"}).status_code == 202
    assert reader.call("DELETE", f"{reader.base}/hosted", params={"discard_unresolved": "true"}).status_code == 200
    local = _write(reader, "marks", {"unit_key": "file:src/a.py", "state": "reviewed"})
    assert local.status_code == 200, local.text
    assert [mark.unit_key for mark in reader.store.list_marks(reader.session.id)] == ["file:src/a.py"]
    # The hosted id no longer routes once detached.
    assert _write(reader, "marks", {"unit_key": "file:src/a.py"}, review=HOSTED_ID).status_code == 404


def test_an_attached_review_is_deleted_by_its_hosted_or_local_identity(reader: _Reader) -> None:
    assert reader.attach().status_code == 200
    reader.store.update_session(reader.session.id, status="archived")
    for wrong in (HOSTED_ID[:12], f"r/{HOSTED_ID}x", "", f"{ORIGIN}/r/{HOSTED_ID}"):
        refused = reader.call("DELETE", f"/api/reviews/{HOSTED_ID}", json={"confirm_identity": wrong})
        assert refused.status_code == 400, (wrong, refused.text)
        assert reader.store.get_session(reader.session.id) is not None
    deleted = reader.call("DELETE", f"/api/reviews/{HOSTED_ID}", json={"confirm_identity": f"r/{HOSTED_ID}"})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["tombstone"]["confirmed_by"] == "id"
    assert reader.store.get_session(reader.session.id) is None
    assert reader.store.get_hosted_attachment(reader.session.id) is None


def test_archiving_an_attached_review_refuses_unsent_hosted_work(reader: _Reader) -> None:
    reader.store.set_current_revision(reader.session.id, reader.revision.id)
    assert reader.attach().status_code == 200
    action_id = _queue_comment(reader)
    archive = {"status": "archived", "expected_revision_id": reader.revision.id}
    for was in ("pending", "conflict"):
        refused = reader.call("POST", f"{reader.base}/finish", json=archive)
        assert refused.status_code == 409 and refused.json()["detail"].startswith("unresolved_actions"), refused.text
        assert reader.store.get_session(reader.session.id).status != "archived"  # type: ignore[union-attr]
        assert reader.store.get_hosted_action(reader.session.id, action_id).state == was  # type: ignore[union-attr]
        if was == "pending":
            reader.op(action_id, {"op": "result", "state": "conflict", "reason": "snapshot_advanced", "result": {}})
    assert reader.op(action_id, {"op": "discard"}).status_code == 200
    archived = reader.call("POST", f"{reader.base}/finish", json=archive)
    assert archived.status_code == 200, archived.text
    assert reader.store.get_session(reader.session.id).status == "archived"  # type: ignore[union-attr]


@pytest.mark.parametrize("resolve", ["discard", "detach"])
def test_deleting_an_attached_review_refuses_unsent_hosted_work_and_audits_it(reader: _Reader, resolve: str) -> None:
    assert reader.attach().status_code == 200
    reader.store.update_session(reader.session.id, status="archived")
    # Work queued before the Review was archived (an older build, or a race) is still unsent.
    unsent, _ = reader.store.add_hosted_action(_action(reader.session.id), unresolved_limit=10)
    confirm = {"confirm_identity": f"r/{HOSTED_ID}"}
    refused = reader.call("DELETE", f"/api/reviews/{HOSTED_ID}", json=confirm)
    assert refused.status_code == 409 and "unresolved_actions" in refused.json()["detail"], refused.text
    assert reader.store.get_session(reader.session.id) is not None
    assert reader.store.get_hosted_action(reader.session.id, unsent.id).state == "pending"  # type: ignore[union-attr]
    assert reader.store.deletion_tombstones(reader.session.id) == ()
    audit = reader.store.root / "audit" / "review-deletions.jsonl"
    outcomes = [json.loads(line)["outcome"] for line in audit.read_text().splitlines()]
    assert outcomes[0] == "deleting" and outcomes[1].startswith("refused: unresolved_actions"), outcomes

    if resolve == "discard":
        assert reader.op(unsent.id, {"op": "discard"}).status_code == 200
        target, confirm = HOSTED_ID, confirm
    else:
        detached = reader.call("DELETE", f"{reader.base}/hosted", params={"discard_unresolved": "true"})
        assert detached.status_code == 200 and detached.json()["discarded_unresolved"] == 1, detached.text
        target, confirm = reader.session.id, {"confirm_identity": reader.session.id}
    deleted = reader.call("DELETE", f"/api/reviews/{target}", json=confirm)
    assert deleted.status_code == 200, deleted.text
    assert reader.store.get_session(reader.session.id) is None


def test_editing_a_comment_is_hosted_owned_while_attached(reader: _Reader) -> None:
    annotation = reader.store.add_annotation(
        Annotation(
            id=new_annotation_id(),
            review_id=reader.session.id,
            revision_id=reader.revision.id,
            anchor=AnnotationAnchor(path="src/a.py", side="new", start_line=0, end_line=0, unit_key="file:src/a.py"),
            body="local note",
        )
    )
    patch = {"body": "edited", "expected_version": 1}
    assert reader.attach().status_code == 200
    refused = reader.call("PATCH", f"/api/annotations/{annotation.id}", json=patch)
    assert refused.status_code == 409 and refused.json()["detail"].startswith("hosted_owned")
    reader.call("DELETE", f"{reader.base}/hosted")
    allowed = reader.call("PATCH", f"/api/annotations/{annotation.id}", json=patch)
    assert not str(allowed.json().get("detail", "")).startswith("hosted_owned"), allowed.text


def test_syncing_a_published_comment_is_hosted_owned_while_attached(reader: _Reader) -> None:
    annotation = reader.store.add_annotation(
        Annotation(
            id=new_annotation_id(),
            review_id=reader.session.id,
            revision_id=reader.revision.id,
            anchor=AnnotationAnchor(path="src/a.py", side="new", start_line=0, end_line=0, unit_key="file:src/a.py"),
            body="local note",
            source="human",
        )
    )
    sync = f"/api/annotations/{annotation.id}/sync"
    for op in ({"op": "edit", "body": "edited"}, {"op": "withdraw"}, {"op": "retry"}, {"op": "discard"}):
        before = reader.call("POST", sync, json=op)
        assert not str(before.json().get("detail", "")).startswith("hosted_owned"), before.text
    assert reader.attach().status_code == 200
    for op in ({"op": "edit", "body": "edited"}, {"op": "withdraw"}, {"op": "retry"}, {"op": "discard"}):
        refused = reader.call("POST", sync, json=op)
        assert refused.status_code == 409 and refused.json()["detail"].startswith("hosted_owned"), refused.text
        assert "online-only" in refused.json()["detail"]
    assert reader.store.list_hosted_actions(reader.session.id) == ()


def test_discarding_an_attached_review_locally_is_not_hosted_owned(reader: _Reader) -> None:
    assert reader.attach().status_code == 200
    archived = reader.call(
        "POST", f"{reader.base}/finish", json={"status": "archived", "expected_revision_id": reader.revision.id}
    )
    assert not str(archived.json().get("detail", "")).startswith("hosted_owned"), archived.text


def test_queueing_is_explicit_bound_to_the_projection_and_bounded(
    reader: _Reader, monkeypatch: pytest.MonkeyPatch
) -> None:
    unattached = reader.queue({"kind": "comment", "path": "src/a.py", "body": "x", "file_level": True})
    assert unattached.status_code == 409 and unattached.json()["detail"].startswith("not_attached")
    assert reader.attach().status_code == 200

    queued = reader.queue({"kind": "mark", "unit_key": "file:src/a.py", "state": "reviewed"})
    assert queued.status_code == 201, queued.text
    action = queued.json()["action"]
    assert (action["kind"], action["state"], action["snapshot_id"], action["sequence"]) == (
        "mark",
        "pending",
        "hs-1",
        1,
    )
    assert action["preconditions"] == {"mark": ["needs_changes", "hs-0"], "fingerprint": "fp-a"}
    assert queued.json()["reference"] == f"r/{HOSTED_ID}"
    again = reader.queue({"kind": "mark", "unit_key": "file:src/a.py", "state": "needs_changes"})
    assert again.json()["superseded"] == [action["id"]]

    mismatch = reader.queue({"kind": "comment", "path": "src/b.py", "body": "x", "file_level": True})
    assert mismatch.status_code == 409 and mismatch.json()["detail"].startswith("content_mismatch")
    invalid = reader.queue({"kind": "comment", "path": "src/a.py", "body": ""})
    assert invalid.status_code == 422 and invalid.json()["detail"].startswith("invalid")

    monkeypatch.setattr(review_api, "UNRESOLVED_ACTION_LIMIT", 2)
    assert reader.queue({"kind": "comment", "path": "src/a.py", "body": "one", "file_level": True}).status_code == 201
    full = reader.queue({"kind": "comment", "path": "src/a.py", "body": "two", "file_level": True})
    assert full.status_code == 409 and full.json()["detail"].startswith("queue_full")

    reader.store.update_session(reader.session.id, status="archived")
    archived = reader.queue({"kind": "comment", "path": "src/a.py", "body": "x", "file_level": True})
    assert archived.status_code == 409 and "discarded and read-only" in archived.json()["detail"]


def test_a_corrupt_stored_projection_refuses_to_queue(reader: _Reader) -> None:
    assert reader.attach().status_code == 200
    with sqlite3.connect(reader.store.db_path) as conn:
        conn.execute("UPDATE review_hosted_attachments SET projection_json = 'not json'")
    refused = reader.queue({"kind": "comment", "path": "src/a.py", "body": "x"})
    assert refused.status_code == 409 and refused.json()["detail"].startswith("invalid_projection")
    with sqlite3.connect(reader.store.db_path) as conn:
        conn.execute("UPDATE review_hosted_attachments SET projection_json = '[]'")
    assert reader.queue({"kind": "comment", "path": "src/a.py", "body": "x"}).status_code == 409
    assert reader.call("GET", f"{reader.base}/hosted").json()["projection"] == []


def _queue_comment(reader: _Reader, body: str = "x") -> str:
    response = reader.queue({"kind": "comment", "path": "src/a.py", "body": body, "file_level": True})
    assert response.status_code == 201, response.text
    return str(response.json()["action"]["id"])


def test_recording_a_hosted_answer_is_validated_and_compare_and_set(reader: _Reader) -> None:
    assert reader.attach().status_code == 200
    action_id = _queue_comment(reader)
    invalid_bodies = [
        {"op": "result", "state": "applied", "reason": "snapshot_advanced", "result": {}},
        {"op": "result", "state": "applied", "reason": "because", "result": {}},
        {"op": "result", "state": "conflict", "reason": "", "result": {}},
        {"op": "result", "state": "conflict", "reason": "because", "result": {}},
        {"op": "result", "state": "merged", "result": {}},
        {"op": "result", "state": "applied", "expected_state": "applied", "result": {}},
        {"op": "result", "state": "applied", "result": ["not", "an", "object"]},
        {"op": "result", "state": "applied", "result": {"blob": "x" * (64 * 1024)}},
        {"op": "rebase"},
    ]
    for body in invalid_bodies:
        response = reader.op(action_id, body)
        assert response.status_code == 422, (body, response.text)
    assert reader.op("rha-missing", {"op": "discard"}).status_code == 404

    recorded = reader.op(action_id, {"op": "result", "state": "conflict", "reason": "snapshot_advanced", "result": {}})
    assert recorded.status_code == 200, recorded.text
    assert (recorded.json()["action"]["state"], recorded.json()["action"]["reason"]) == (
        "conflict",
        "snapshot_advanced",
    )
    assert recorded.json()["counts"]["conflict"] == 1
    stale = reader.op(action_id, {"op": "result", "state": "applied", "result": {}})
    assert stale.status_code == 409 and stale.json()["detail"].startswith("action_state_changed")

    discarded = reader.op(action_id, {"op": "discard"})
    assert discarded.status_code == 200
    assert (discarded.json()["action"]["state"], discarded.json()["action"]["reason"]) == (
        "discarded",
        "discarded_by_reviewer",
    )
    # Hosted answered for an action discarded while in flight: recorded over the discard.
    over = reader.op(action_id, {"op": "result", "expected_state": "discarded", "state": "applied", "result": {}})
    assert over.status_code == 200 and over.json()["action"]["state"] == "applied"
    again = reader.op(action_id, {"op": "discard"})
    assert again.status_code == 409 and again.json()["detail"].startswith("action_state_changed")


def test_requeue_is_explicit_revalidated_and_atomic(reader: _Reader) -> None:
    assert reader.attach().status_code == 200
    action_id = _queue_comment(reader, "on S1")
    not_conflict = reader.op(action_id, {"op": "requeue"})
    assert not_conflict.status_code == 409 and not_conflict.json()["detail"].startswith("action_state_changed")
    # A pending action is never re-prepared: the refusal names its state even once hosted closed.
    assert reader.attach(projection=_projection(status="finished")).status_code == 200
    closed = reader.op(action_id, {"op": "requeue"})
    assert closed.status_code == 409 and closed.json()["detail"].startswith("action_state_changed")
    assert reader.attach().status_code == 200
    reader.op(action_id, {"op": "result", "state": "conflict", "reason": "snapshot_advanced", "result": {}})

    # Hosted moved to S2 and src/a.py changed there: requeue is refused and nothing moves.
    s2_changed = _projection(snapshot={"id": "hs-2", "number": 1}, files={"src/a.py": "fp-a2"})
    assert reader.attach(projection=s2_changed).status_code == 200
    refused = reader.op(action_id, {"op": "requeue"})
    assert refused.status_code == 409 and refused.json()["detail"].startswith("content_mismatch")
    assert reader.store.get_hosted_action(reader.session.id, action_id).state == "conflict"  # type: ignore[union-attr]
    assert len(reader.store.list_hosted_actions(reader.session.id)) == 1

    # On an S2 that kept src/a.py, the reviewer's explicit requeue binds to S2.
    assert reader.attach(projection=_projection(snapshot={"id": "hs-2", "number": 1})).status_code == 200
    requeued = reader.op(action_id, {"op": "requeue"})
    assert requeued.status_code == 200, requeued.text
    fresh = requeued.json()["action"]
    assert (fresh["state"], fresh["snapshot_id"], fresh["payload"]["body"]) == ("pending", "hs-2", "on S1")
    old = reader.store.get_hosted_action(reader.session.id, action_id)
    assert old is not None and (old.state, old.reason) == ("discarded", "requeued")
    assert json.loads(old.result_json) == {
        "resolution": "requeued",
        "requeued_as": fresh["id"],
        "conflict": "snapshot_advanced",
    }


def test_a_requeue_that_cannot_be_queued_keeps_the_conflict(reader: _Reader, monkeypatch: pytest.MonkeyPatch) -> None:
    assert reader.attach().status_code == 200
    conflicted = _queue_comment(reader, "on S1")
    reader.op(conflicted, {"op": "result", "state": "conflict", "reason": "snapshot_advanced", "result": {}})
    _queue_comment(reader, "two")
    _queue_comment(reader, "three")
    monkeypatch.setattr(review_api, "UNRESOLVED_ACTION_LIMIT", 2)
    refused = reader.op(conflicted, {"op": "requeue"})
    assert refused.status_code == 409 and refused.json()["detail"].startswith("queue_full")
    kept = reader.store.get_hosted_action(reader.session.id, conflicted)
    assert kept is not None and (kept.state, kept.reason) == ("conflict", "snapshot_advanced")
    assert len(reader.store.list_hosted_actions(reader.session.id)) == 3


def test_requeue_of_a_request_completion_is_not_a_duplicate_of_itself(reader: _Reader) -> None:
    assert reader.attach().status_code == 200
    queued = reader.queue({"kind": "request_complete", "request_id": "req-1"})
    action_id = queued.json()["action"]["id"]
    reader.op(action_id, {"op": "result", "state": "conflict", "reason": "request_changed", "result": {}})
    changed = _projection(my_requests={"req-1": {"state": "active", "version": 5, "scope_kind": "all"}})
    assert reader.attach(projection=changed).status_code == 200
    requeued = reader.op(action_id, {"op": "requeue"})
    assert requeued.status_code == 200, requeued.text
    assert requeued.json()["action"]["preconditions"] == {"request_version": 5}


def test_every_queue_write_prunes_resolved_actions(reader: _Reader, monkeypatch: pytest.MonkeyPatch) -> None:
    assert reader.attach().status_code == 200
    first, second, third = (_queue_comment(reader, str(index)) for index in range(3))
    monkeypatch.setattr(review_api, "RESOLVED_ACTION_KEEP", 2)
    for action_id in (first, second, third):
        reader.op(action_id, {"op": "result", "state": "applied", "result": {}})
    # Recording an answer prunes past the bound...
    assert [row.id for row in reader.store.list_hosted_actions(reader.session.id)] == [second, third]
    # ...and so does queueing.
    monkeypatch.setattr(review_api, "RESOLVED_ACTION_KEEP", 1)
    fourth = _queue_comment(reader, "4")
    assert [row.id for row in reader.store.list_hosted_actions(reader.session.id)] == [third, fourth]


def test_detach_is_explicit_and_says_what_it_keeps(reader: _Reader) -> None:
    assert reader.call("DELETE", f"{reader.base}/hosted").json()["detail"].startswith("not_attached")
    assert reader.attach().status_code == 200
    _queue_comment(reader)
    refused = reader.call("DELETE", f"{reader.base}/hosted")
    assert refused.status_code == 409 and refused.json()["detail"].startswith("unresolved_actions")
    detached = reader.call("DELETE", f"{reader.base}/hosted", params={"discard_unresolved": "true"})
    assert detached.status_code == 200, detached.text
    assert detached.json()["discarded_unresolved"] == 1
    assert "local Review" in detached.json()["kept"] and "stays on hosted" in detached.json()["kept"]
    assert reader.call("GET", f"{reader.base}/hosted").json()["attached"] is False
    assert reader.call("GET", f"/api/reviews/{HOSTED_ID}/hosted").status_code == 404


# --------------------------------------------------------------------------- #
# client: hosted credentials are fail-closed
# --------------------------------------------------------------------------- #


def _client_env(tmp_path: Path, url: str, **extra: str) -> dict[str, str]:
    return {"LEMONCROW_HOME": str(tmp_path / "home"), "LEMONCROW_URL": url, "HOME": str(tmp_path), **extra}


def _local(tmp_path: Path) -> Any:
    from lemoncrow_client.config import load_config

    return load_config(_client_env(tmp_path, "http://127.0.0.1:8765"), cwd=tmp_path)


def test_hosted_credentials_are_the_managed_login_for_a_configured_hosted_origin(tmp_path: Path) -> None:
    from lemoncrow_client.credentials import managed_credentials_path, write_managed_credentials
    from lemoncrow_client.errors import ClientError, ErrorCode

    from lemoncrow.pro.capabilities.review.hosted_attachment_client import hosted_attachment_config

    local = _local(tmp_path)
    hosted_env = _client_env(tmp_path, "https://hosted.example")
    with pytest.raises(ClientError) as no_login:
        hosted_attachment_config(local, "", env=hosted_env)
    assert no_login.value.code == ErrorCode.UNAUTHENTICATED

    # A legacy login in the state dir (as load_config reads it) serves until an account login exists.
    write_managed_credentials(tmp_path / "home", access_token="legacy-access-token-0123456789", refresh_token="r" * 32)
    assert hosted_attachment_config(local, "", env=hosted_env).token == "legacy-access-token-0123456789"

    account = tmp_path / "home" / "account"
    write_managed_credentials(account, access_token="managed-access-token-0123456789", refresh_token="r" * 32)
    config = hosted_attachment_config(local, "", env=hosted_env)
    assert config.token == "managed-access-token-0123456789"
    assert config.token_source == str(managed_credentials_path(account))
    assert config.refresh_token == "r" * 32
    assert config.hosted and config.url.startswith("https://hosted.example")
    assert hosted_attachment_config(local, "https://HOSTED.example/", env=hosted_env).token == config.token

    # An operator token in the environment is never what an attachment sends.
    operator = hosted_attachment_config(
        local, "", env={**hosted_env, "LEMONCROW_TOKEN": "operator-token-0123456789abcdef"}
    )
    assert operator.token == "managed-access-token-0123456789"

    with pytest.raises(ClientError) as elsewhere:
        hosted_attachment_config(local, "https://evil.example", env=hosted_env)
    assert elsewhere.value.code == ErrorCode.NOT_CONFIGURED
    with pytest.raises(ClientError) as nothing_hosted:
        hosted_attachment_config(local, "", env=_client_env(tmp_path, "http://127.0.0.1:8765"))
    assert nothing_hosted.value.code == ErrorCode.NOT_CONFIGURED

    write_managed_credentials(account, access_token="lc_local_0123456789abcdef0123", refresh_token="r" * 32)
    with pytest.raises(ClientError) as local_token:
        hosted_attachment_config(local, "", env=hosted_env)
    assert local_token.value.code == ErrorCode.UNAUTHENTICATED


def test_several_hosted_servers_need_an_explicit_origin(tmp_path: Path) -> None:
    from lemoncrow_client.credentials import write_managed_credentials
    from lemoncrow_client.errors import ClientError, ErrorCode

    from lemoncrow.pro.capabilities.review.hosted_attachment_client import hosted_attachment_config

    home = tmp_path / "home"
    for product, url in (("review", "https://a.example"), ("runtime", "https://b.example")):
        (home / product).mkdir(parents=True)
        (home / product / "env").write_text(f"LEMONCROW_URL={url}\n", encoding="utf-8")
    write_managed_credentials(home / "account", access_token="managed-access-token-0123456789", refresh_token="r" * 32)
    env = {"LEMONCROW_HOME": str(home), "HOME": str(tmp_path)}
    with pytest.raises(ClientError, match="several hosted") as ambiguous:
        hosted_attachment_config(_local(tmp_path), "", env=env)
    assert ambiguous.value.code == ErrorCode.NOT_CONFIGURED
    assert hosted_attachment_config(_local(tmp_path), "https://b.example", env=env).url.startswith("https://b.example")


def test_hosted_references_parse_from_ref_url_or_id() -> None:
    from lemoncrow_client.errors import ClientError

    from lemoncrow.pro.capabilities.review.hosted_attachment_client import parse_hosted_reference

    assert parse_hosted_reference(f"r/{HOSTED_ID}") == HOSTED_ID
    assert parse_hosted_reference(f"https://hosted.example/r/{HOSTED_ID}/") == HOSTED_ID
    assert parse_hosted_reference(HOSTED_ID) == HOSTED_ID
    for bad in ("", "r/", "r/../x y", "bad id!", "..", "r/..", "x/abc", "https://hosted.example/", "https://h/x/abc"):
        with pytest.raises(ClientError):
            parse_hosted_reference(bad)


def test_hosted_errors_map_to_offline_rejected_or_stop() -> None:
    from lemoncrow_client.errors import ClientError, ErrorCode

    from lemoncrow.pro.capabilities.review.hosted_attachment_client import _hosted_error

    assert isinstance(_hosted_error(ClientError(ErrorCode.SERVER_UNREACHABLE, "down")), HostedUnavailable)
    assert isinstance(_hosted_error(ClientError(ErrorCode.UNKNOWN, "busy", retryable=True)), HostedUnavailable)
    for code, mapped in (
        (ErrorCode.PAYLOAD_INVALID, "rejected"),
        (ErrorCode.FORBIDDEN, "forbidden"),
        (ErrorCode.UNAUTHENTICATED, "unauthenticated"),
        (ErrorCode.REVIEW_UNKNOWN, "review_unknown"),
    ):
        error = _hosted_error(ClientError(code, "no"))
        assert isinstance(error, HostedReplayRefused) and error.code == mapped


def test_sync_never_sends_a_hosted_login_to_another_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from lemoncrow.pro.capabilities.review import hosted_attachment_client as client_side

    events: list[str] = []
    attachment: dict[str, Any] = {
        "origin": ORIGIN,
        "hosted_repo_id": "hosted-repo",
        "hosted_review_id": HOSTED_ID,
        "principal_id": "alice",
    }

    class _LocalPort:
        def __init__(self, config: Any, review_id: str) -> None:
            events.append(f"local:{review_id}")

        def status(self) -> Mapping[str, Any]:
            return {"attachment": attachment}

        def close(self) -> None:
            events.append("local-closed")

    class _HostedPort:
        def __init__(self, config: Any, repo_id: str, review_id: str) -> None:
            events.append(f"hosted:{config.url}:{repo_id}:{review_id}")

        def close(self) -> None:
            events.append("hosted-closed")

    monkeypatch.setattr(client_side, "ReaderLocalPort", _LocalPort)
    monkeypatch.setattr(client_side, "RemoteHostedPort", _HostedPort)
    monkeypatch.setattr(client_side, "reconnect", lambda local, hosted: engine.ReconnectReport(identity="ok"))

    other = client_side.sync_review(object(), SimpleNamespace(url="https://evil.example"), "local-review")  # type: ignore[arg-type]
    assert other.identity == "origin_mismatch"
    assert events == ["local:local-review", "local-closed"]

    events.clear()
    same = client_side.sync_review(object(), SimpleNamespace(url="https://HOSTED.example/"), "local-review")  # type: ignore[arg-type]
    assert same.identity == "ok"
    assert events == [
        "local:local-review",
        f"hosted:https://HOSTED.example/:hosted-repo:{HOSTED_ID}",
        "hosted-closed",
        "local-closed",
    ]

    attachment.clear()
    events.clear()
    assert client_side.sync_review(object(), SimpleNamespace(url=ORIGIN), "local-review").identity == "not_attached"  # type: ignore[arg-type]
    assert events == ["local:local-review", "local-closed"]


def test_a_lost_compare_and_set_on_the_local_reader_is_an_attachment_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from lemoncrow_client.errors import ClientError, ErrorCode

    from lemoncrow.pro.capabilities.review import hosted_attachment_client as client_side

    refusal: list[ClientError] = []

    class _Session:
        def __init__(self, config: Any) -> None:
            pass

        def review_reader_request(self, method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
            raise refusal[0]

        def close(self) -> None:
            pass

    monkeypatch.setattr(client_side, "RemoteSession", _Session)
    port = client_side.ReaderLocalPort(SimpleNamespace(tool_timeout_s=1.0), "local-review")  # type: ignore[arg-type]
    refusal.append(ClientError(ErrorCode.REVIEW_STATE_CONFLICT, "action_state_changed: moved"))
    with pytest.raises(HostedAttachmentConflict) as lost:
        port.record_result("rha-1", expected_state="pending", state="applied", reason="", result={})
    assert lost.value.code == "action_state_changed"
    refusal[0] = ClientError(ErrorCode.FORBIDDEN, "no")
    with pytest.raises(ClientError):
        port.record_result("rha-1", expected_state="pending", state="applied", reason="", result={})


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_the_hosted_repository_is_found_for_exactly_this_review(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from lemoncrow_client.errors import ClientError, ErrorCode

    from lemoncrow.pro.capabilities.review import hosted_attachment_client as client_side

    inbox: list[dict[str, Any]] = [
        {"id": "another-hosted-review", "repo_id": "repo-other"},
        {"id": HOSTED_ID, "repo_id": ""},
    ]

    class _Session:
        def __init__(self, config: Any) -> None:
            pass

        def review_inbox(self, **kwargs: Any) -> Mapping[str, Any]:
            return {"reviews": inbox}

        def close(self) -> None:
            pass

    monkeypatch.setattr(client_side, "RemoteSession", _Session)
    hosted = SimpleNamespace(url=ORIGIN, request_timeout_s=1.0)
    with pytest.raises(ClientError) as unknown:
        client_side.attach_review(SimpleNamespace(), hosted, "local-review", f"r/{HOSTED_ID}")  # type: ignore[arg-type]
    assert unknown.value.code == ErrorCode.REVIEW_UNKNOWN
    inbox.append({"id": HOSTED_ID, "repo_id": "repo-mine"})
    assert client_side.RemoteHostedPort(hosted, "_", HOSTED_ID).find_repo_id() == "repo-mine"  # type: ignore[arg-type]


def test_lc_review_hosted_records_only_on_the_local_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import click

    from lemoncrow.gateway.cli.commands import review_hosted_cli

    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LEMONCROW_INSTALL_MODE", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LEMONCROW_URL", "https://hosted.example")
    with pytest.raises(click.ClickException, match="points at hosted"):
        review_hosted_cli._local_config()
    monkeypatch.setenv("LEMONCROW_URL", "http://127.0.0.1:8765")
    assert review_hosted_cli._local_config().hosted is False


def test_lc_review_hosted_is_a_review_subcommand_and_queues_exactly_what_was_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from lemoncrow.gateway.cli.commands import review_hosted_cli
    from lemoncrow.gateway.cli.commands.review_record_cli import REVIEW_SUBCOMMANDS
    from lemoncrow.pro.capabilities.review import hosted_attachment_client

    assert REVIEW_SUBCOMMANDS["hosted"] is review_hosted_cli.hosted_group
    sent: list[tuple[str, Any]] = []

    class _Port:
        def __init__(self, config: Any, review_id: str) -> None:
            sent.append(("review", review_id))

        def queue(self, body: Mapping[str, Any]) -> dict[str, Any]:
            sent.append(("queue", dict(body)))
            return {"action": {"id": "rha-1", "kind": body["kind"]}, "superseded": ["rha-0"], "reference": "r/h"}

        def close(self) -> None:
            sent.append(("closed", None))

    monkeypatch.setattr(review_hosted_cli, "_local_config", lambda: object())
    monkeypatch.setattr(hosted_attachment_client, "ReaderLocalPort", _Port)
    result = CliRunner().invoke(
        review_hosted_cli.hosted_group, ["queue", "r/local-review", "comment", "--path", "a.py", "--body", "hi"]
    )
    assert result.exit_code == 0, result.output
    assert sent == [
        ("review", "local-review"),
        ("queue", {"kind": "comment", "path": "a.py", "body": "hi", "file_level": True}),
        ("closed", None),
    ]
    assert "Queued comment rha-1 for hosted r/h." in result.output and "Superseded rha-0." in result.output

    sent.clear()
    reply = CliRunner().invoke(
        review_hosted_cli.hosted_group, ["queue", "local-review", "reply", "--parent", "ann-1", "--body", "ok"]
    )
    assert reply.exit_code == 0, reply.output
    assert sent[1] == ("queue", {"kind": "reply", "parent_id": "ann-1", "body": "ok"})


def test_lc_review_hosted_sync_prints_each_exact_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from lemoncrow.gateway.cli.commands import review_hosted_cli
    from lemoncrow.pro.capabilities.review import hosted_attachment_client

    report = engine.ReconnectReport(
        results=[
            {"action_id": "rha-1", "kind": "mark", "state": "applied", "reason": ""},
            {"action_id": "rha-2", "kind": "reply", "state": "conflict", "reason": "thread_deleted"},
        ],
        stopped="offline",
        remaining=1,
    )
    monkeypatch.setattr(review_hosted_cli, "_local_config", lambda: object())
    monkeypatch.setattr(review_hosted_cli, "_hosted_config", lambda local, origin: object())
    monkeypatch.setattr(hosted_attachment_client, "sync_review", lambda local, hosted, review: report)
    result = CliRunner().invoke(review_hosted_cli.hosted_group, ["sync", "local-review"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "rha-1  mark  applied",
        "rha-2  reply  conflict (thread_deleted)",
        "Stopped: offline; 1 action(s) still pending.",
    ]
    report.identity = "mismatch"
    refused = CliRunner().invoke(review_hosted_cli.hosted_group, ["sync", "local-review"])
    assert refused.exit_code != 0 and "nothing replayed: mismatch" in refused.output


def test_a_later_mark_is_refused_at_queue_time_until_the_hosted_server_says_it_records_it() -> None:
    """P1-2 H: queue time, not sync time, so the reviewer is told before anything waits."""
    aligned = _projection(targets={"file:src/a.py": {"path": "src/a.py", "state": "unreviewed", "fingerprint": "fp-a"}})
    body = {"unit_key": "file:src/a.py", "state": "later"}
    for projection in (aligned, {**aligned, "later_marks": False}):
        with pytest.raises(engine.HostedActionRefused) as refused:
            _prepare("mark", body, projection)
        assert refused.value.code == "invalid"
        assert "not available on hosted reviews yet" in str(refused.value)
    # The next schema bump flips the hosted flag and nothing here changes; other states never needed it.
    opened = _prepare("mark", body, {**aligned, "later_marks": True})
    assert opened is not None
    assert _prepare("mark", {**body, "state": "needs_changes"}, aligned) is not None
