"""P2-4 Wrap up: exact human-selected feedback preview and delivery contract."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.feedback import (
    FeedbackBundle,
    FeedbackItem,
    FeedbackPresentation,
    render_markdown,
)
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore

TOKEN = "review-wrap-test-token"


def _item(annotation_id: str, kind: str = "request_change") -> FeedbackItem:
    return FeedbackItem(
        annotation_id=annotation_id,
        kind=kind,
        state="open",
        path="src/charge.py",
        start_line=4,
        end_line=5,
        body="Keep the guard",
        symbol="charge",
    )


def test_legacy_export_does_not_change_when_presentation_is_absent() -> None:
    legacy = FeedbackBundle(review_id="review", revision_id="rev", revision_number=0, items=(_item("a"),))
    expected = (
        "## Review feedback\n\nRevision 1 · \n\n### src/charge.py:L4-L5 — request change\n"
        "<!-- lemoncrow-annotation-id: a -->\n*in `charge` · file unchanged since the comment*\n\n"
        "Keep the guard\n\nHuman review REQUIRED. Nothing here is a verdict.\n"
    )
    assert render_markdown(legacy) == expected
    assert render_markdown(replace(legacy, presentation=None)) == expected


def test_wrap_format_has_exact_verdict_selected_note_importance_and_test_request() -> None:
    source = FeedbackBundle(
        review_id="review",
        revision_id="rev",
        revision_number=0,
        items=(_item("a"), _item("q")),
        presentation=FeedbackPresentation(
            verdict="changes_requested",
            importance=(("a", "must_fix"), ("q", "question")),
            test_with_fix=True,
            summary="Correct the access guard",
        ),
    )
    message = render_markdown(source)
    assert message.startswith("## Review of Revision 1 — Request changes")
    assert "### MUST FIX · src/charge.py:L4-L5 · charge" in message
    assert "### QUESTION · src/charge.py:L4-L5 · charge" in message
    assert "Correct the access guard" in message
    assert message.count("Add a test that proves this.") == 1
    assert message.endswith("Human review. Nothing here was decided by an AI.\n")
    assert render_markdown(source) == message


def test_wrap_formats_orphan_without_claiming_an_exact_line() -> None:
    source = FeedbackBundle(
        review_id="review",
        revision_id="rev",
        revision_number=0,
        orphaned=(_item("orphan"),),
        presentation=FeedbackPresentation(verdict="lgtm", importance=(("orphan", "suggestion"),)),
    )
    message = render_markdown(source)
    assert "SUGGESTION · src/charge.py (anchor not verified)" in message
    assert "src/charge.py:L4-L5" not in message


@pytest.fixture
def review(tmp_path: Path) -> tuple[TestClient, str, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Review Wrap"
    repo.config["user.email"] = "wrap@example.invalid"
    path = root / "charge.py"
    path.write_text("def charge():\n    return 1\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("Review Wrap", "wrap@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", sig, sig, "baseline", tree, [])
    path.write_text("def charge():\n    return 2\n", encoding="utf-8")
    root_store = tmp_path / "store"
    store = ReviewStore(root_store)
    rng = resolve_rev_range(root, working_tree=True)
    built = build_review_packet_with_blobs(root, rng, store_root=root_store, with_patch_text=True)
    session = open_or_create_session(store, root, rng, title=built.packet.title)
    first = snapshot_revision(store, session, root, rng, store_root=root_store, build=built)
    store.publish_snapshot(session.id, first.id)
    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency(TOKEN), repo_root=root, port=48080)
    client = TestClient(app, base_url="http://127.0.0.1:48080")
    posted = client.post(
        f"/api/reviews/{session.id}/annotations",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "path": "charge.py",
            "start_line": 1,
            "body": "Keep the guard",
            "kind": "request_change",
            "expected_revision_id": first.id,
        },
    )
    assert posted.status_code == 201, posted.text
    return client, session.id, first.id, posted.json()["annotation"]["id"]


def test_api_preview_is_bound_to_human_importance_and_revision(review: tuple[TestClient, str, str, str]) -> None:
    client, review_id, revision, note_id = review
    endpoint = f"/api/reviews/{review_id}/feedback/export"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    legacy = client.post(endpoint, headers=headers)
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["markdown"].startswith("## Review feedback")
    human = {
        "verdict": "changes_requested",
        "importance": {note_id: "must_fix"},
        "test_with_fix": True,
        "summary": "Cover the failure case",
    }
    reply = client.post(endpoint, headers=headers, json={"expected_revision_id": revision, "presentation": human})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["revision_id"] == revision
    assert body["presentation"] == human
    assert "### MUST FIX" in body["markdown"]
    assert "Add a test that proves this." in body["markdown"]
    assert "Cover the failure case" in body["markdown"]
    assert body["feedback_hash"] != legacy.json()["feedback_hash"]
    assert body["operation_id"] != legacy.json()["operation_id"]
    # A differently formatted or stale message cannot be delivered under this digest.
    deliver = f"/api/reviews/{review_id}/feedback/deliver"
    bad = client.post(
        deliver,
        headers=headers,
        json={
            "operation_id": body["operation_id"],
            "expected_revision_id": revision,
            "expected_feedback_hash": body["feedback_hash"],
            "presentation": {**human, "test_with_fix": False},
            "copied": True,
        },
    )
    assert bad.status_code == 409, bad.text
    # The same approved immutable preview is copied-for-author, never routed to an unverified agent.
    copied = client.post(
        deliver,
        headers=headers,
        json={
            "operation_id": body["operation_id"],
            "expected_revision_id": revision,
            "expected_feedback_hash": body["feedback_hash"],
            "presentation": human,
            "copied": True,
        },
    )
    assert copied.status_code == 200, copied.text
    assert copied.json()["annotation_count"] == 1
    # A retry of the same operation with altered presentation must not deliver other bytes.
    altered = client.post(
        deliver,
        headers=headers,
        json={
            "operation_id": body["operation_id"],
            "expected_revision_id": revision,
            "expected_feedback_hash": body["feedback_hash"],
            "presentation": {**human, "test_with_fix": False},
            "copied": True,
        },
    )
    assert altered.status_code == 409, altered.text
    assert "differs from the prepared preview" in altered.text
    same = client.post(
        deliver,
        headers=headers,
        json={
            "operation_id": body["operation_id"],
            "expected_revision_id": revision,
            "expected_feedback_hash": body["feedback_hash"],
            "presentation": human,
            "copied": True,
        },
    )
    assert same.status_code == 200, same.text
    assert (
        client.post(
            endpoint, headers=headers, json={"expected_revision_id": "stale", "presentation": human}
        ).status_code
        == 409
    )
    assert (
        client.post(
            endpoint, headers=headers, json={"presentation": {**human, "importance": {"wrong": "must_fix"}}}
        ).status_code
        == 409
    )
    for invalid in [
        {"verdict": []},
        {"verdict": "lgtm", "importance": {"a": []}},
        {"verdict": "lgtm", "test_with_fix": "yes"},
    ]:
        assert client.post(endpoint, headers=headers, json={"presentation": invalid}).status_code == 422


def test_retry_after_the_note_was_edited_is_refused_before_the_claim(review: tuple[TestClient, str, str, str]) -> None:
    client, review_id, revision, note_id = review
    headers = {"Authorization": f"Bearer {TOKEN}"}
    human = {"verdict": "changes_requested", "importance": {note_id: "must_fix"}, "test_with_fix": False, "summary": ""}
    prepared = client.post(
        f"/api/reviews/{review_id}/feedback/export",
        headers=headers,
        json={"expected_revision_id": revision, "presentation": human},
    ).json()
    request = {
        "operation_id": prepared["operation_id"],
        "expected_revision_id": revision,
        "expected_feedback_hash": prepared["feedback_hash"],
        "presentation": human,
        "copied": True,
    }
    deliver = f"/api/reviews/{review_id}/feedback/deliver"
    assert client.post(deliver, headers=headers, json=request).status_code == 200
    edited = client.patch(
        f"/api/annotations/{note_id}",
        headers=headers,
        json={"body": "A different request", "expected_revision_id": revision},
    )
    assert edited.status_code == 200, edited.text
    retry = client.post(deliver, headers=headers, json=request)
    assert retry.status_code == 409, retry.text
    assert "differs from the prepared preview" in retry.text


def _no_note_review(review: tuple[TestClient, str, str, str]) -> tuple[TestClient, str, str]:
    client, review_id, revision, note_id = review
    response = client.patch(
        f"/api/annotations/{note_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"state": "resolved", "expected_revision_id": revision},
    )
    assert response.status_code == 200, response.text
    return client, review_id, revision


def test_flow_send_cannot_silently_skip_unpublished_human_notes(
    review: tuple[TestClient, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, review_id, revision, _ = review
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "100")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    send = client.post(
        f"/api/reviews/{review_id}/flow-send",
        headers=headers,
        json={"expected_revision_id": revision, "outcome": "lgtm", "summary": "", "feedback": None},
    )
    assert send.status_code == 409
    assert "unpublished human notes" in send.text


def _prepared_agent_feedback(
    review: tuple[TestClient, str, str, str], monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, str, str, dict[str, Any]]:
    """A prepared preview for a verified agent destination, the only kind Undo supports."""

    client, review_id, revision, note_id = review
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "100")
    monkeypatch.setattr("lemoncrow.pro.capabilities.review.delivery.direct_delivery_supported", lambda host: True)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    pinned = client.put(
        f"/api/reviews/{review_id}/destination",
        headers=headers,
        json={"kind": "session", "host": "claude", "session_id": "session-author"},
    )
    assert pinned.status_code == 200, pinned.text
    human = {"verdict": "changes_requested", "importance": {note_id: "must_fix"}, "test_with_fix": False, "summary": ""}
    prepared = client.post(
        f"/api/reviews/{review_id}/feedback/export",
        headers=headers,
        json={"expected_revision_id": revision, "presentation": human},
    )
    assert prepared.status_code == 200, prepared.text
    return client, review_id, revision, prepared.json()


@pytest.mark.parametrize("field", ["feedback_hash", "operation_id"])
def test_flow_send_refuses_a_preview_whose_hash_or_operation_id_changed(
    review: tuple[TestClient, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    client, review_id, revision, prepared = _prepared_agent_feedback(review, monkeypatch)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    endpoint = f"/api/reviews/{review_id}/flow-send"
    feedback = {key: prepared[key] for key in ("revision_id", "operation_id", "feedback_hash", "presentation")}
    body = {"expected_revision_id": revision, "outcome": "changes_requested", "summary": "", "feedback": feedback}
    altered = {**body, "feedback": {**feedback, field: "0" * len(feedback[field])}}
    refused = client.post(endpoint, headers=headers, json=altered)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == "the exact feedback preview changed; review it again"
    # Nothing was queued, so nothing can be sent later.
    assert client.get(endpoint, headers=headers).json()["state"] == "none"
    time.sleep(0.13)
    asyncio.run(client.app.state.review_flow_dispatch_due())
    assert client.get(f"/api/reviews/{review_id}/outcome", headers=headers).json()["current"] is None
    # Control: the same request with the shown bytes is accepted, so the 409 above was the mismatch.
    accepted = client.post(endpoint, headers=headers, json=body)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"] == "pending"


def test_flow_send_undo_is_a_server_side_compare_and_swap(
    review: tuple[TestClient, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, review_id, revision = _no_note_review(review)
    # The window must outlast five sequential requests on a loaded machine; the test waits it out below.
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "2000")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    endpoint = f"/api/reviews/{review_id}/flow-send"
    scheduled = client.post(
        endpoint,
        headers=headers,
        json={"expected_revision_id": revision, "outcome": "lgtm", "summary": "", "feedback": None},
    )
    assert scheduled.status_code == 200, scheduled.text
    payload = scheduled.json()
    assert payload["state"] == "pending"
    assert payload["send_after"]
    assert client.get(endpoint, headers=headers).json()["operation_id"] == payload["operation_id"]
    assert client.get(f"/api/reviews/{review_id}/outcome", headers=headers).json()["current"] is None
    # An active send is unique; a second tab cannot double-submit a verdict.
    duplicate = client.post(
        endpoint,
        headers=headers,
        json={"expected_revision_id": revision, "outcome": "comment", "summary": "", "feedback": None},
    )
    assert duplicate.status_code == 409
    undone = client.post(f"{endpoint}/{payload['operation_id']}/undo", headers=headers)
    assert undone.status_code == 200, undone.text
    assert undone.json()["state"] == "undone"
    assert client.post(f"{endpoint}/{payload['operation_id']}/undo", headers=headers).status_code == 409
    # Past the send deadline, so a worker that still claimed an undone row would deliver it now.
    time.sleep(2.1)
    asyncio.run(client.app.state.review_flow_dispatch_due())
    assert client.get(endpoint, headers=headers).json()["state"] == "undone"
    assert client.get(f"/api/reviews/{review_id}/outcome", headers=headers).json()["current"] is None


def test_flow_send_dispatches_only_after_deadline_and_is_not_undoable(
    review: tuple[TestClient, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, review_id, revision = _no_note_review(review)
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "100")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    endpoint = f"/api/reviews/{review_id}/flow-send"
    started = client.post(
        endpoint,
        headers=headers,
        json={"expected_revision_id": revision, "outcome": "lgtm", "summary": "", "feedback": None},
    )
    assert started.status_code == 200, started.text
    operation_id = started.json()["operation_id"]
    asyncio.run(client.app.state.review_flow_dispatch_due())
    assert client.get(endpoint, headers=headers).json()["state"] == "pending"
    assert client.get(f"/api/reviews/{review_id}/outcome", headers=headers).json()["current"] is None
    time.sleep(0.13)
    assert client.post(f"{endpoint}/{operation_id}/undo", headers=headers).status_code == 409
    asyncio.run(client.app.state.review_flow_dispatch_due())
    status = client.get(endpoint, headers=headers)
    assert status.status_code == 200
    assert status.json()["state"] == "sent", status.text
    outcome = client.get(f"/api/reviews/{review_id}/outcome", headers=headers).json()["current"]
    assert outcome["outcome"] == "lgtm"
    assert client.get(f"/api/reviews/{review_id}/finish", headers=headers).json()["status"] == "finished"
    asyncio.run(client.app.state.review_flow_dispatch_due())
    assert client.get(endpoint, headers=headers).json()["state"] == "sent"


def test_flow_send_worker_runs_during_server_lifespan(
    review: tuple[TestClient, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, review_id, revision = _no_note_review(review)
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "100")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    endpoint = f"/api/reviews/{review_id}/flow-send"
    with client:
        started = client.post(
            endpoint,
            headers=headers,
            json={
                "expected_revision_id": revision,
                "outcome": "lgtm",
                "summary": "",
                "feedback": None,
            },
        )
        assert started.status_code == 200, started.text
        for _ in range(16):
            if client.get(endpoint, headers=headers).json()["state"] == "sent":
                break
            time.sleep(0.08)
        assert client.get(endpoint, headers=headers).json()["state"] == "sent"


def test_flow_send_client_operation_id_is_idempotent_even_after_dispatch(
    review: tuple[TestClient, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, review_id, revision = _no_note_review(review)
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "100")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    endpoint = f"/api/reviews/{review_id}/flow-send"
    request = {
        "client_operation_id": "fs-" + "a" * 32,
        "expected_revision_id": revision,
        "outcome": "lgtm",
        "summary": "Reviewer approval",
        "feedback": None,
    }
    first = client.post(endpoint, headers=headers, json=request)
    assert first.status_code == 200, first.text
    assert first.json()["operation_id"] == request["client_operation_id"]
    repeated = client.post(endpoint, headers=headers, json=request)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["operation_id"] == first.json()["operation_id"]
    assert repeated.json()["send_after"] == first.json()["send_after"]
    altered = client.post(endpoint, headers=headers, json={**request, "summary": "different"})
    assert altered.status_code == 409, altered.text
    time.sleep(0.13)
    asyncio.run(client.app.state.review_flow_dispatch_due())
    completed = client.post(endpoint, headers=headers, json=request)
    assert completed.status_code == 200, completed.text
    assert completed.json()["state"] == "sent"
    assert completed.json()["operation_id"] == first.json()["operation_id"]
    assert client.get(f"/api/reviews/{review_id}/outcome", headers=headers).json()["current"]["outcome"] == "lgtm"


def test_flow_send_store_scopes_claim_to_repository_and_review(
    review: tuple[TestClient, str, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker for another checkout cannot claim this Review's due send."""
    client, review_id, revision = _no_note_review(review)
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "100")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    scheduled = client.post(
        f"/api/reviews/{review_id}/flow-send",
        headers=headers,
        json={
            "expected_revision_id": revision,
            "outcome": "lgtm",
            "summary": "",
            "feedback": None,
        },
    )
    assert scheduled.status_code == 200, scheduled.text
    time.sleep(0.13)
    store = ReviewStore(tmp_path / "store")
    session = store.get_session(review_id)
    assert session is not None
    assert store.claim_due_review_flow_sends(now="9999-01-01T00:00:00+00:00", repo_root="wrong") == ()
    assert store.claim_due_review_flow_sends(now="9999-01-01T00:00:00+00:00", review_id="wrong") == ()
    claimed = store.claim_due_review_flow_sends(
        now="9999-01-01T00:00:00+00:00",
        repo_root=session.repo_root,
        review_id=review_id,
    )
    assert len(claimed) == 1
    assert claimed[0]["id"] == scheduled.json()["operation_id"]
    assert store.claim_due_review_flow_sends(now="9999-01-01T00:00:00+00:00") == ()


def test_flow_send_fails_closed_when_human_notes_change_while_waiting(
    review: tuple[TestClient, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, review_id, revision = _no_note_review(review)
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "100")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    endpoint = f"/api/reviews/{review_id}/flow-send"
    started = client.post(
        endpoint,
        headers=headers,
        json={"expected_revision_id": revision, "outcome": "lgtm", "summary": "", "feedback": None},
    )
    assert started.status_code == 200, started.text
    created = client.post(
        f"/api/reviews/{review_id}/annotations",
        headers=headers,
        json={
            "path": "charge.py",
            "start_line": 1,
            "body": "New requirement",
            "kind": "request_change",
            "expected_revision_id": revision,
        },
    )
    assert created.status_code == 201, created.text
    time.sleep(0.13)
    asyncio.run(client.app.state.review_flow_dispatch_due())
    assert client.get(endpoint, headers=headers).json()["state"] == "failed"
    assert client.get(f"/api/reviews/{review_id}/outcome", headers=headers).json()["current"] is None


def test_flow_send_survives_a_late_enrichment_of_the_same_snapshot(
    review: tuple[TestClient, str, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evidence, gaps and ranks that land during the hold are LemonCrow's, not the reviewer's: the send goes."""

    from lemoncrow.pro.capabilities.review.models import review_packet_from_dict
    from lemoncrow.pro.capabilities.review.sources.local import encode_packet, read_packet_json

    client, review_id, revision = _no_note_review(review)
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "100")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    endpoint = f"/api/reviews/{review_id}/flow-send"
    started = client.post(
        endpoint,
        headers=headers,
        json={"expected_revision_id": revision, "outcome": "lgtm", "summary": "", "feedback": None},
    )
    assert started.status_code == 200, started.text
    store = ReviewStore(tmp_path / "store")
    stored = store.get_revision(revision)
    assert stored is not None
    packet = read_packet_json(store, stored)
    assert packet is not None
    packet["evidence"] = [*packet.get("evidence", []), {"name": "pytest", "status": "PASS"}]
    store.update_revision_projection(
        revision,
        packet_payload=encode_packet(review_packet_from_dict(packet)),
        packet_schema_version=stored.packet_schema_version,
        degraded=(*stored.degraded, "centrality"),
        provenance_host="codex",
        provenance_model="late-model",
        provenance_session_id="late-session",
        provenance_certainty="exact",
        units=tuple(replace(unit, attention_rank=unit.attention_rank + 7) for unit in store.list_units(revision)),
    )
    time.sleep(0.13)
    asyncio.run(client.app.state.review_flow_dispatch_due())
    status = client.get(endpoint, headers=headers).json()
    assert status["state"] == "sent", status
    assert client.get(f"/api/reviews/{review_id}/outcome", headers=headers).json()["current"]["outcome"] == "lgtm"


def test_flow_send_stale_dispatching_becomes_uncertain_and_allows_a_later_send(
    review: tuple[TestClient, str, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash mid-dispatch must not leave the review unsendable, nor resend on its own."""
    client, review_id, revision = _no_note_review(review)
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "100")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    endpoint = f"/api/reviews/{review_id}/flow-send"
    body = {"expected_revision_id": revision, "outcome": "lgtm", "summary": "", "feedback": None}
    first = client.post(endpoint, headers=headers, json=body)
    assert first.status_code == 200, first.text
    time.sleep(0.13)
    store = ReviewStore(tmp_path / "store")
    session = store.get_session(review_id)
    assert session is not None
    claimed = store.claim_due_review_flow_sends(now="2999-01-01T00:00:00+00:00", repo_root=session.repo_root)
    assert len(claimed) == 1  # the worker "dies" here: the row stays dispatching
    assert client.get(endpoint, headers=headers).json()["state"] == "dispatching"
    assert client.post(endpoint, headers=headers, json=body).status_code == 409
    assert store.recover_stale_review_flow_sends(before="9999-01-01T00:00:00+00:00") == 1
    recovered = client.get(endpoint, headers=headers).json()
    assert recovered["state"] == "uncertain"
    assert recovered["error"]
    # Never auto-dispatched, and nothing left to recover.
    assert store.claim_due_review_flow_sends(now="9999-01-01T00:00:00+00:00") == ()
    assert store.recover_stale_review_flow_sends(before="9999-01-01T00:00:00+00:00") == 0
    later = client.post(endpoint, headers=headers, json=body)
    assert later.status_code == 200, later.text
    assert later.json()["operation_id"] != first.json()["operation_id"]
    assert later.json()["state"] == "pending"


def test_flow_send_hold_override_is_ignored_outside_a_test_environment(
    review: tuple[TestClient, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    client, review_id, revision = _no_note_review(review)
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "100")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("LEMONCROW_E2E_AGENT_LOG_DIR", raising=False)
    response = client.post(
        f"/api/reviews/{review_id}/flow-send",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"expected_revision_id": revision, "outcome": "lgtm", "summary": "", "feedback": None},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    hold = datetime.fromisoformat(body["send_after"]) - datetime.fromisoformat(body["created_at"])
    assert 9 <= hold.total_seconds() <= 11
    assert body["note_count"] == 0
