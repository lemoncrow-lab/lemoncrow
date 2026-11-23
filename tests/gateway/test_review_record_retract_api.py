"""P6-2: the retract route, end to end on the real server: sign, store, refuse after 24 hours, verify, reopen."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lemoncrow.core.settings import write_setting
from lemoncrow.pro.capabilities.review.record_retraction import RETRACTION_RELATIONSHIP_KIND
from lemoncrow.pro.capabilities.review.record_signing import USER_KEY_SETTING, generate_user_key
from tests.gateway.test_review_record_send_signing import (
    HEADERS,
    _opt_in,
    _records,
    _send,
    _wait_records,
    key_root,  # noqa: F401
)
from tests.gateway.test_review_wrap_feedback import _no_note_review, review  # noqa: F401


def _signed(
    review_tuple: tuple[TestClient, str, str, str], outcome: str = "lgtm"
) -> tuple[TestClient, str, dict[str, Any]]:
    client, review_id, revision = _no_note_review(review_tuple)
    _send(client, review_id, revision, outcome)
    [record] = _wait_records(client, review_id, 1)
    return client, review_id, record


def _retract(client: TestClient, review_id: str, record_id: str) -> Any:
    return client.post(f"/api/reviews/{review_id}/records/{record_id}/retract", headers=HEADERS)


def _verify(client: TestClient, review_id: str, record_id: str) -> dict[str, Any]:
    body: dict[str, Any] = client.get(f"/api/reviews/{review_id}/records/{record_id}/verify", headers=HEADERS).json()
    return body


def test_a_fresh_record_says_when_it_can_be_retracted_until(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    _client, _id, record = _signed(review)
    assert record["retractable"] is True and record["retracted"] is None
    signed_at = time.mktime(time.strptime(record["issued_at"][:19], "%Y-%m-%dT%H:%M:%S"))
    until = time.mktime(time.strptime(record["retract_until"][:19], "%Y-%m-%dT%H:%M:%S"))
    assert until - signed_at == 24 * 3600  # REC-5: the same clock as Signed at, plus 24 hours


def test_retracting_signs_stores_and_reopens(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, record = _signed(review)
    assert client.get(f"/api/reviews/{review_id}/finish", headers=HEADERS).json()["status"] == "finished"
    done = _retract(client, review_id, record["record_id"])
    assert done.status_code == 201, done.text
    body = done.json()
    assert body["retraction"]["created"] is True and body["record"]["retracted"]["by"] == record["decided_by"]
    # The Review is open again, so Wrap up can send a new verdict.
    assert client.get(f"/api/reviews/{review_id}/finish", headers=HEADERS).json()["status"] == "open"
    [after] = _wait_records(client, review_id, 1)
    assert after["retractable"] is False and after["retracted"]["at"] == body["retraction"]["at"]
    envelope = client.get(f"/api/reviews/{review_id}/records/{record['record_id']}/retraction", headers=HEADERS)
    assert envelope.status_code == 200
    assert json.loads(envelope.text)["payloadType"] == "application/vnd.in-toto+json"


def test_a_retracted_record_verifies_as_retracted_with_its_line_and_details(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, record = _signed(review)
    before = _verify(client, review_id, record["record_id"])
    assert before["outcome"]["outcome"] == "valid"
    rows_before = {row["label"]: row["value"] for row in before["details"]}
    assert rows_before["Retract"].startswith("until ") and rows_before["Retract"].endswith(" UTC (signed time + 24 h)")
    assert _retract(client, review_id, record["record_id"]).status_code == 201
    after = _verify(client, review_id, record["record_id"])
    assert (after["outcome"]["outcome"], after["outcome"]["exit_code"]) == ("retracted", 5)
    assert (
        after["outcome"]["headline"]
        == f"Retracted · by @{record['decided_by']} at {after['outcome']['retraction']['at'][11:16]} UTC"
    )
    rows = {row["label"]: row["value"] for row in after["details"]}
    assert rows["Retract"].startswith("retracted at ") and rows["Retract"].endswith("by you")
    # The record's own file is the same bytes as before.
    assert after["record"]["payload_digest"] == record["payload_digest"]


def test_a_second_retract_answers_with_the_first_and_changes_nothing(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, record = _signed(review)
    first = _retract(client, review_id, record["record_id"]).json()
    second = _retract(client, review_id, record["record_id"])
    assert second.status_code == 201
    assert second.json()["retraction"] == {**first["retraction"], "created": False}


def test_the_server_refuses_a_retraction_after_the_window_whatever_the_client_shows(
    review: tuple[TestClient, str, str, str], key_root: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    _opt_in(key_root)
    monkeypatch.setenv("LEMONCROW_REVIEW_RETRACT_MS", "1500")
    client, review_id, record = _signed(review)
    assert record["retractable"] is True
    time.sleep(1.6)
    refused = _retract(client, review_id, record["record_id"])
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "window_closed" and detail["retract_until"]
    after = _wait_records(client, review_id, 1)[0]
    assert after["retractable"] is False and after["retracted"] is None
    assert (
        client.get(f"/api/reviews/{review_id}/records/{record['record_id']}/retraction", headers=HEADERS).status_code
        == 404
    )
    assert _verify(client, review_id, record["record_id"])["outcome"]["outcome"] == "valid"


def test_the_window_override_is_ignored_outside_a_test(
    review: tuple[TestClient, str, str, str], key_root: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, record = _signed(review)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("LEMONCROW_E2E_AGENT_LOG_DIR", raising=False)
    monkeypatch.setenv("LEMONCROW_REVIEW_RETRACT_MS", "100")
    time.sleep(0.3)
    assert _retract(client, review_id, record["record_id"]).status_code == 201


def test_only_the_key_that_signed_the_record_can_retract_it(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, record = _signed(review)
    # Another key is now the one configured: it did not sign this record.
    other = key_root / "review-signing" / "other.pem"
    other_id = generate_user_key(other)
    write_setting(key_root, USER_KEY_SETTING, str(other))
    assert other_id != record["key_id"]
    refused = _retract(client, review_id, record["record_id"])
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "different_key"
    assert _wait_records(client, review_id, 1)[0]["retractable"] is False


def test_without_a_signing_key_nothing_is_retracted(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, record = _signed(review)
    write_setting(key_root, USER_KEY_SETTING, "")
    refused = _retract(client, review_id, record["record_id"])
    assert refused.status_code == 409
    assert (
        client.get(f"/api/reviews/{review_id}/records/{record['record_id']}/retraction", headers=HEADERS).status_code
        == 404
    )


def test_a_record_nobody_signed_is_not_found(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, _record = _signed(review)
    assert _retract(client, review_id, "rec_" + "0" * 32).status_code == 404


def test_sending_again_after_a_retraction_signs_a_new_record(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, record = _signed(review)
    assert _retract(client, review_id, record["record_id"]).status_code == 201
    revision = client.get(f"/api/reviews/{review_id}/record", headers=HEADERS).json()["snapshot_id"]
    _send(client, review_id, revision, "lgtm")
    records = _wait_records(client, review_id, 2)
    assert len(records) == 2
    newest = max(records, key=lambda row: row["issued_at"])
    assert newest["record_id"] != record["record_id"] and newest["retracted"] is None and newest["retractable"] is True
    assert _verify(client, review_id, record["record_id"])["outcome"]["outcome"] == "retracted"
    assert _verify(client, review_id, newest["record_id"])["outcome"]["outcome"] == "valid"


def test_the_retraction_is_one_row_of_its_own_kind_that_goes_with_the_change(
    review: tuple[TestClient, str, str, str], key_root: Path, tmp_path: Path  # noqa: F811
) -> None:
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    _opt_in(key_root)
    client, review_id, record = _signed(review)
    assert _retract(client, review_id, record["record_id"]).status_code == 201
    _retract(client, review_id, record["record_id"])
    store = ReviewStore(tmp_path / "store")
    assert len(store.list_relationships(review_id, kind=RETRACTION_RELATIONSHIP_KIND)) == 1
    # Its closer: the row is the Change's own, so deleting the Change deletes it.
    revision = client.get(f"/api/reviews/{review_id}/record", headers=HEADERS).json()["snapshot_id"]
    archived = client.post(
        f"/api/reviews/{review_id}/finish",
        headers=HEADERS,
        json={"status": "archived", "expected_revision_id": revision},
    )
    assert archived.status_code == 200, archived.text
    assert store.delete_archived_session(review_id) is True
    assert store.list_relationships(review_id, kind=RETRACTION_RELATIONSHIP_KIND) == ()


# --------------------------------------------------------------------------- #
# a retracted approval is not an approval
# --------------------------------------------------------------------------- #


def _approval_condition(client: TestClient, review_id: str) -> dict[str, Any]:
    body = client.get(f"/api/reviews/{review_id}/readiness", headers=HEADERS)
    assert body.status_code == 200, body.text
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("id") == "human-approval-state":
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(body.json())
    assert found, "readiness carries the human approval condition"
    return found[0]


def test_a_retracted_approval_no_longer_counts_until_the_person_sends_again(
    review: tuple[TestClient, str, str, str], key_root: Path, tmp_path: Path  # noqa: F811
) -> None:
    from lemoncrow.pro.capabilities.review.readiness import approval_records
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    _opt_in(key_root)
    client, review_id, record = _signed(review)
    revision = record["revision_id"]
    store = ReviewStore(tmp_path / "store")
    standing = _approval_condition(client, review_id)
    assert standing["state"] == "pass" and standing["detail"].startswith("1 current human LGTM outcome(s)")
    assert len(approval_records(store, review_id, revision)) == 1

    assert _retract(client, review_id, record["record_id"]).status_code == 201
    retracted = _approval_condition(client, review_id)
    assert retracted["state"] == "not_applicable"
    assert approval_records(store, review_id, revision) == ()

    _send(client, review_id, revision, "lgtm")
    _wait_records(client, review_id, 2)
    again = _approval_condition(client, review_id)
    assert again["state"] == "pass" and again["detail"].startswith("1 current human LGTM outcome(s)")
    assert len(approval_records(store, review_id, revision)) == 1


def test_a_retracted_request_for_changes_no_longer_blocks(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, record = _signed(review, "changes_requested")
    assert _approval_condition(client, review_id)["state"] == "fail"
    assert _retract(client, review_id, record["record_id"]).status_code == 201
    assert _approval_condition(client, review_id)["state"] == "not_applicable"


def test_an_explicit_export_does_not_mint_a_standing_record_of_a_retracted_verdict(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, record = _signed(review)
    assert _retract(client, review_id, record["record_id"]).status_code == 201
    exported = client.post(f"/api/reviews/{review_id}/record", headers=HEADERS, json={"signing": "auto"})
    assert exported.status_code == 409
    assert "retracted" in exported.text
    assert len(_records(client, review_id)) == 1


# --------------------------------------------------------------------------- #
# who may retract, and the guards every write has
# --------------------------------------------------------------------------- #


def _no_retraction(client: TestClient, review_id: str, record_id: str) -> None:
    assert client.get(f"/api/reviews/{review_id}/records/{record_id}/retraction", headers=HEADERS).status_code == 404


def test_only_the_person_who_decided_can_retract(
    review: tuple[TestClient, str, str, str], key_root: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from lemoncrow.pro.capabilities.review.record_signing import LocalRecordSigning

    _opt_in(key_root)
    client, review_id, record = _signed(review)
    monkeypatch.setattr(LocalRecordSigning, "exporter", lambda self, session: ("someone-else", "human"))
    refused = _retract(client, review_id, record["record_id"])
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "not_your_verdict"
    _no_retraction(client, review_id, record["record_id"])
    assert _records(client, review_id)[0]["retractable"] is False


def test_a_discarded_change_cannot_be_retracted(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, record = _signed(review)
    archived = client.post(
        f"/api/reviews/{review_id}/finish",
        headers=HEADERS,
        json={"status": "archived", "expected_revision_id": record["revision_id"]},
    )
    assert archived.status_code == 200, archived.text
    refused = _retract(client, review_id, record["record_id"])
    assert refused.status_code == 409 and "discarded" in refused.json()["detail"]
    _no_retraction(client, review_id, record["record_id"])


def test_a_review_whose_judgments_belong_to_hosted_cannot_be_retracted_here(
    review: tuple[TestClient, str, str, str], key_root: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from types import SimpleNamespace

    from lemoncrow.pro.capabilities.review.store import ReviewStore

    _opt_in(key_root)
    client, review_id, record = _signed(review)
    monkeypatch.setattr(ReviewStore, "hosted_attachment_supported", lambda self: True)
    monkeypatch.setattr(ReviewStore, "get_hosted_attachment", lambda self, _id: SimpleNamespace(hosted_review_id="h-1"))
    refused = _retract(client, review_id, record["record_id"])
    assert refused.status_code == 409 and refused.json()["detail"].startswith("hosted_owned")
    monkeypatch.undo()
    _no_retraction(client, review_id, record["record_id"])


def test_a_key_that_is_not_your_own_cannot_retract(
    review: tuple[TestClient, str, str, str], key_root: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from dataclasses import replace

    from lemoncrow.pro.capabilities.review.record_signing import LocalRecordSigning

    _opt_in(key_root)
    client, review_id, record = _signed(review)
    original = LocalRecordSigning.status
    monkeypatch.setattr(
        LocalRecordSigning, "status", lambda self, session: replace(original(self, session), mode="organization_key")
    )
    refused = _retract(client, review_id, record["record_id"])
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "not_signed_here"
    monkeypatch.undo()
    _no_retraction(client, review_id, record["record_id"])


# --------------------------------------------------------------------------- #
# reopening
# --------------------------------------------------------------------------- #


def _status_of(client: TestClient, review_id: str) -> str:
    return str(client.get(f"/api/reviews/{review_id}/finish", headers=HEADERS).json()["status"])


def _finish(client: TestClient, review_id: str, revision: str) -> None:
    done = client.post(
        f"/api/reviews/{review_id}/finish",
        headers=HEADERS,
        json={"status": "finished", "expected_revision_id": revision},
    )
    assert done.status_code == 200, done.text


def test_a_repeat_retract_reopens_a_review_that_is_still_finished(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    """A reopen that failed once (the retraction stood) is retried by the next Retract."""

    _opt_in(key_root)
    client, review_id, record = _signed(review)
    assert _retract(client, review_id, record["record_id"]).status_code == 201
    assert _status_of(client, review_id) == "open"
    _finish(client, review_id, record["revision_id"])  # as if the first reopen never happened
    assert _status_of(client, review_id) == "finished"
    again = _retract(client, review_id, record["record_id"])
    assert again.status_code == 201 and again.json()["retraction"]["created"] is False
    assert _status_of(client, review_id) == "open"


def test_an_old_retraction_never_reopens_a_review_with_a_new_approval(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, record = _signed(review)
    assert _retract(client, review_id, record["record_id"]).status_code == 201
    _send(client, review_id, record["revision_id"], "lgtm")
    _wait_records(client, review_id, 2)
    assert _status_of(client, review_id) == "finished"
    assert _retract(client, review_id, record["record_id"]).json()["retraction"]["created"] is False
    assert _status_of(client, review_id) == "finished"


# --------------------------------------------------------------------------- #
# a retracted verdict is no decision
# --------------------------------------------------------------------------- #


def _store_beside(client: TestClient, tmp_path: Path) -> Any:
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    del client
    return ReviewStore(tmp_path / "store")


def _stand_auto_approval(store: Any, review_id: str, revision_id: str) -> None:
    from lemoncrow.pro.capabilities.review.auto_approval import AUTO_APPROVAL_RELATION
    from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship

    store.upsert_relationship(
        ReviewRelationship(
            id="",
            review_id=review_id,
            kind="landing",
            relation=AUTO_APPROVAL_RELATION,
            provider="github",
            external_id="auto_approval",
            title="Auto-approval",
            status="approved",
            source="policy",
            metadata_json=json.dumps(
                {
                    "revision_id": revision_id,
                    "decided_at": "2026-11-08T09:00:00+00:00",
                    "decided_by": "lemoncrow-auto-approval",
                    "decision_digest": "decision:" + "a" * 64,
                    "decision": {},
                }
            ),
        )
    )


def test_a_provider_merge_never_signs_the_retracted_approval(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, record = _signed(review)
    assert _retract(client, review_id, record["record_id"]).status_code == 201
    merged = client.app.state.review_record_on_merge(review_id)  # type: ignore[attr-defined]
    assert merged["status"] == "no_decision"
    assert len(_records(client, review_id)) == 1


def test_a_merge_after_a_resend_signs_again_because_the_approval_stands(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, record = _signed(review)
    assert _retract(client, review_id, record["record_id"]).status_code == 201
    _send(client, review_id, record["revision_id"], "lgtm")
    _wait_records(client, review_id, 2)
    assert client.app.state.review_record_on_merge(review_id)["status"] == "recorded"  # type: ignore[attr-defined]


def test_an_auto_approval_on_the_revision_is_still_exportable_after_a_retraction(
    review: tuple[TestClient, str, str, str], key_root: Path, tmp_path: Path  # noqa: F811
) -> None:
    from lemoncrow.pro.capabilities.review.review_record import current_decision

    _opt_in(key_root)
    client, review_id, record = _signed(review)
    assert _retract(client, review_id, record["record_id"]).status_code == 201
    store = _store_beside(client, tmp_path)
    _stand_auto_approval(store, review_id, record["revision_id"])
    session, revision = store.get_session(review_id), store.latest_revision(review_id)
    decision = current_decision(store, session, revision)
    assert decision is not None and (decision.kind, decision.outcome) == ("landing", "auto_approved")
    exported = client.post(f"/api/reviews/{review_id}/record", headers=HEADERS, json={"signing": "auto"})
    assert exported.status_code == 201, exported.text
    assert exported.json()["record"]["decision_kind"] == "landing"


def test_a_resend_is_being_signed_before_its_record_exists(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    """The record line must say Signing, not failure, while a resent verdict's signature is made."""

    import threading

    _opt_in(key_root)
    client, review_id, record = _signed(review)
    assert _retract(client, review_id, record["record_id"]).status_code == 201
    release, entered = threading.Event(), threading.Event()
    real = client.app.state.review_record_signer_at_send  # type: ignore[attr-defined]

    def slow(review_id_: str, outcome: Any) -> dict[str, Any]:
        entered.set()
        release.wait(15)
        return real(review_id_, outcome)  # type: ignore[no-any-return]

    client.app.state.review_record_signer_at_send = slow  # type: ignore[attr-defined]
    _send(client, review_id, record["revision_id"], "lgtm")
    assert entered.wait(10)
    try:
        status_now = client.get(f"/api/reviews/{review_id}/record", headers=HEADERS).json()
        assert status_now["signing_due"] is True
    finally:
        release.set()
    _wait_records(client, review_id, 2)


# --------------------------------------------------------------------------- #
# break-glass follow-up
# --------------------------------------------------------------------------- #


def test_a_retracted_approval_does_not_complete_a_break_glass_follow_up(
    review: tuple[TestClient, str, str, str], key_root: Path, tmp_path: Path  # noqa: F811
) -> None:
    from datetime import UTC, datetime

    from lemoncrow.pro.capabilities.review.break_glass import BreakGlassRequest, record_break_glass
    from lemoncrow.pro.capabilities.review.inbox_inputs import _open_follow_ups
    from lemoncrow.pro.capabilities.review.landing_api import landing_extras

    _opt_in(key_root)
    client, review_id, record = _signed(review)
    store = _store_beside(client, tmp_path)
    session, revision = store.get_session(review_id), store.latest_revision(review_id)
    request = BreakGlassRequest(
        reason="outage", incident_url="", bypass=(), expected_revision_id=revision.id, expected_conditions_hash=""
    )
    record_break_glass(
        store,
        review_id=review_id,
        revision=revision,
        actor_id="responder",
        request=request,
        bypassed=[],
        readiness={},
        follow_up_hours=24,
        now="2020-01-01T00:00:00+00:00",
    )
    now = datetime(2026, 11, 8, tzinfo=UTC)

    def state() -> str:
        return str(
            landing_extras(store, session, revision, now=now.isoformat())["break_glass"][0]["follow_up"]["state"]
        )

    def open_count() -> int:
        return _open_follow_ups(store, [revision.id], {revision.id: review_id}, now=now).get(review_id, 0)

    # A standing approval by someone else closes the follow-up; the same approval retracted does not.
    assert (state(), open_count()) == ("completed", 0)
    assert _retract(client, review_id, record["record_id"]).status_code == 201
    assert (state(), open_count()) == ("overdue", 1)
    _send(client, review_id, record["revision_id"], "lgtm")
    _wait_records(client, review_id, 2)
    assert (state(), open_count()) == ("completed", 0)


def test_an_older_verdict_does_not_take_a_retracted_verdicts_place(
    review: tuple[TestClient, str, str, str], key_root: Path, tmp_path: Path  # noqa: F811
) -> None:
    """Request changes, then Approve, then retract the Approve: nothing stands, for readiness and for a record."""

    from lemoncrow.pro.capabilities.review.readiness import approval_records
    from lemoncrow.pro.capabilities.review.record_retraction import standing_outcomes
    from lemoncrow.pro.capabilities.review.review_record import current_decision

    _opt_in(key_root)
    client, review_id, first = _signed(review, "changes_requested")
    _send(client, review_id, first["revision_id"], "lgtm")
    records = _wait_records(client, review_id, 2)
    approval = next(row for row in records if row["decision"] == "approved")
    assert _retract(client, review_id, approval["record_id"]).status_code == 201

    store = _store_beside(client, tmp_path)
    session, revision = store.get_session(review_id), store.latest_revision(review_id)
    assert standing_outcomes(store, review_id) == ()
    assert approval_records(store, review_id, revision.id) == ()
    assert current_decision(store, session, revision) is None
    exported = client.post(f"/api/reviews/{review_id}/record", headers=HEADERS, json={"signing": "auto"})
    assert exported.status_code == 409
    assert client.app.state.review_record_on_merge(review_id)["status"] == "no_decision"  # type: ignore[attr-defined]
    assert len(_records(client, review_id)) == 2
    # Sending the approval again makes it stand, and the older request for changes stays out of the way.
    _send(client, review_id, first["revision_id"], "lgtm")
    _wait_records(client, review_id, 3)
    decision = current_decision(store, session, revision)
    assert decision is not None and decision.outcome == "approved"


# --------------------------------------------------------------------------- #
# P6-1 O: signing is "due" from before the verdict is stored until its signer has started
# --------------------------------------------------------------------------- #


def test_a_verdict_is_due_for_signing_between_its_store_write_and_the_start_of_its_signer(
    review: tuple[TestClient, str, str, str], key_root: Path, tmp_path: Path  # noqa: F811
) -> None:
    """The window: the verdict is already stored, its signer has not started. A read here must say "signing"."""

    _opt_in(key_root)
    client, review_id, revision = _no_note_review(review)
    store = _store_beside(client, tmp_path)
    app_state = client.app.state  # type: ignore[attr-defined]
    seen: dict[str, Any] = {}
    original = app_state.review_sign_outcome

    def at_the_window(outcome: Any) -> None:
        seen["stored"] = [row.id for row in store.list_outcomes(review_id)]
        seen["outcome"] = outcome.id
        seen["in_flight"] = app_state.review_verdict_in_flight(review_id)
        original(outcome)

    app_state.review_sign_outcome = at_the_window
    assert app_state.review_verdict_in_flight(review_id) is False
    posted = client.post(
        f"/api/reviews/{review_id}/outcome",
        headers=HEADERS,
        json={"outcome": "lgtm", "summary": "", "expected_revision_id": revision},
    )
    assert posted.status_code == 200, posted.text
    assert seen["outcome"] in seen["stored"] and seen["in_flight"] is True
    assert app_state.review_verdict_in_flight(review_id) is False  # dropped as soon as the signer was started
    [signed] = _wait_records(client, review_id, 1)
    assert signed["decision"] == "approved"


def test_a_verdict_that_fails_to_store_leaves_no_marker_behind(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    _opt_in(key_root)
    client, review_id, _revision = _no_note_review(review)
    refused = client.post(
        f"/api/reviews/{review_id}/outcome",
        headers=HEADERS,
        json={"outcome": "lgtm", "summary": "", "expected_revision_id": "not-the-revision"},
    )
    assert refused.status_code >= 400
    assert client.app.state.review_verdict_in_flight(review_id) is False  # type: ignore[attr-defined]
    assert client.get(f"/api/reviews/{review_id}/record", headers=HEADERS).json()["signing_due"] is False


def test_the_status_says_signing_while_a_verdict_is_in_flight_and_failure_once_it_is_not(
    review: tuple[TestClient, str, str, str], key_root: Path, tmp_path: Path  # noqa: F811
) -> None:
    """A stored verdict with no record: "signing" exactly while the server says its signer is coming or running."""

    from fastapi import APIRouter, FastAPI

    from lemoncrow.pro.capabilities.review.record_signing import LocalRecordSigning
    from lemoncrow.pro.capabilities.review.review_record_api import RecordDeps, register_review_record_routes
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    _opt_in(key_root)
    _client, review_id, revision_id = _no_note_review(review)
    store = ReviewStore(tmp_path / "store")
    store.record_outcome(
        review_id, revision_id, "local", outcome="lgtm", summary="", expected_current_revision_id=revision_id
    )
    flags = {"in_flight": False, "running": False}

    def unused(*_args: Any) -> Any:
        raise AssertionError("not needed to read the status")

    deps = RecordDeps(
        store=store,
        session=lambda _id: store.get_session(review_id),
        latest=lambda _session: store.latest_revision(review_id),
        display_number=lambda _revision: 0,
        readiness_at=unused,
        policy_generation=unused,
        run_plan=unused,
        authority=LocalRecordSigning(settings_root=lambda: key_root),
        repository=unused,
        now=lambda: "2026-11-08T10:00:00+00:00",
        signing_due=lambda _ref: flags["running"],
        verdict_in_flight=lambda _id: flags["in_flight"],
    )
    router = APIRouter(prefix="/api")
    register_review_record_routes(router, deps)
    app = FastAPI()
    app.include_router(router)
    mini = TestClient(app)

    def due() -> bool:
        body = mini.get(f"/api/reviews/{review_id}/record").json()
        assert body["records"] == [] and body["signing"]["available"] is True
        return bool(body["signing_due"])

    assert due() is False  # stored, no signer coming, none running: a real failure to sign
    flags["in_flight"] = True
    assert due() is True  # the window between the store write and the start of the signer
    flags.update(in_flight=False, running=True)
    assert due() is True  # the signer is running
    flags["running"] = False
    assert due() is False


def test_signing_is_seen_by_another_api_instance_of_the_same_review(
    review: tuple[TestClient, str, str, str], key_root: Path, tmp_path: Path  # noqa: F811
) -> None:
    """The local server builds one API instance per Review for the send worker and others for requests.

    A request answered by a different instance than the one signing must still say "signing" (it did not: the
    state was per instance, so Done's first ask saw "not signing" for a verdict being signed, then stopped asking).
    """

    import threading

    from fastapi import FastAPI

    from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
    from lemoncrow.pro.capabilities.review.store import ReviewStore
    from tests.gateway.test_review_wrap_feedback import TOKEN

    _opt_in(key_root)
    client, review_id, revision = _no_note_review(review)
    other = FastAPI()
    register_review_api(
        other,
        ReviewStore(tmp_path / "store"),
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=tmp_path / "repo",
        port=48080,
    )
    elsewhere = TestClient(other, base_url="http://127.0.0.1:48080")
    assert elsewhere.app is not client.app

    release, entered = threading.Event(), threading.Event()
    real = client.app.state.review_record_signer_at_send  # type: ignore[attr-defined]

    def slow(review_id_: str, outcome: Any) -> dict[str, Any]:
        entered.set()
        release.wait(15)
        return real(review_id_, outcome)  # type: ignore[no-any-return]

    client.app.state.review_record_signer_at_send = slow  # type: ignore[attr-defined]
    _send(client, review_id, revision, "lgtm")
    assert entered.wait(10)
    try:
        asked = elsewhere.get(f"/api/reviews/{review_id}/record", headers=HEADERS).json()
        assert asked["records"] == [] and asked["signing_due"] is True
    finally:
        release.set()
    [signed] = _wait_records(elsewhere, review_id, 1)
    assert signed["decision"] == "approved"
    assert elsewhere.get(f"/api/reviews/{review_id}/record", headers=HEADERS).json()["signing_due"] is False


def test_a_signer_thread_that_cannot_start_leaves_nothing_marked_as_signing(
    review: tuple[TestClient, str, str, str], key_root: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    import threading

    from lemoncrow.pro.capabilities.review import api as review_api

    _opt_in(key_root)
    client, review_id, revision = _no_note_review(review)
    real_start = threading.Thread.start

    def start(self: threading.Thread) -> None:
        if self.name.startswith("sign-verdict-"):
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    posted = client.post(
        f"/api/reviews/{review_id}/outcome",
        headers=HEADERS,
        json={"outcome": "lgtm", "summary": "", "expected_revision_id": revision},
    )
    assert posted.status_code == 200, posted.text  # the verdict itself is stored whatever happens to its signer
    assert review_api._SIGNING_NOW == set()
    status = client.get(f"/api/reviews/{review_id}/record", headers=HEADERS).json()
    assert (status["records"], status["signing_due"]) == ([], False)
    assert client.app.state.review_verdict_in_flight(review_id) is False  # type: ignore[attr-defined]


def _outcome_by(store: Any, review_id: str, revision_id: str, reviewer: str, outcome: str) -> None:
    store.record_outcome(
        review_id, revision_id, reviewer, outcome=outcome, summary="", expected_current_revision_id=revision_id
    )


def test_a_comment_after_a_retracted_approval_does_not_bring_back_an_older_request_for_changes(
    review: tuple[TestClient, str, str, str], key_root: Path, tmp_path: Path  # noqa: F811
) -> None:
    """Request changes, Approve (retracted), then a comment: the comment is no verdict, so nothing stands."""

    from lemoncrow.pro.capabilities.review.readiness import approval_records
    from lemoncrow.pro.capabilities.review.record_retraction import standing_outcomes
    from lemoncrow.pro.capabilities.review.review_record import current_decision

    _opt_in(key_root)
    client, review_id, first = _signed(review, "changes_requested")
    _send(client, review_id, first["revision_id"], "lgtm")
    approval = next(row for row in _wait_records(client, review_id, 2) if row["decision"] == "approved")
    assert _retract(client, review_id, approval["record_id"]).status_code == 201
    store = _store_beside(client, tmp_path)
    _outcome_by(store, review_id, first["revision_id"], "local", "comment")
    session, revision = store.get_session(review_id), store.latest_revision(review_id)
    assert current_decision(store, session, revision) is None
    assert [item.outcome for item in standing_outcomes(store, review_id)] == [
        "comment"
    ]  # a comment, which no one counts
    assert approval_records(store, review_id, revision.id) == ()
    exported = client.post(f"/api/reviews/{review_id}/record", headers=HEADERS, json={"signing": "auto"})
    assert exported.status_code == 409
    assert _approval_condition(client, review_id)["state"] == "not_applicable"


@pytest.mark.parametrize("other_first", [False, True])
def test_one_reviewers_retraction_leaves_another_reviewers_verdict_standing(
    review: tuple[TestClient, str, str, str], key_root: Path, tmp_path: Path, other_first: bool  # noqa: F811
) -> None:
    from lemoncrow.pro.capabilities.review.readiness import approval_records
    from lemoncrow.pro.capabilities.review.record_retraction import standing_outcomes
    from lemoncrow.pro.capabilities.review.review_record import current_decision

    _opt_in(key_root)
    client, review_id, revision = _no_note_review(review)
    store = _store_beside(client, tmp_path)
    if other_first:
        _outcome_by(store, review_id, revision, "alice", "lgtm")
    _send(client, review_id, revision, "lgtm")
    [mine] = [row for row in _wait_records(client, review_id, 1) if row["decided_by"] != "alice"]
    if not other_first:
        _outcome_by(store, review_id, revision, "alice", "lgtm")
    assert _retract(client, review_id, mine["record_id"]).status_code == 201

    session, latest = store.get_session(review_id), store.latest_revision(review_id)
    standing = standing_outcomes(store, review_id)
    assert [(item.reviewer_id, item.outcome) for item in standing] == [("alice", "lgtm")]
    assert [item.reviewer_id for item in approval_records(store, review_id, latest.id)] == ["alice"]
    decision = current_decision(store, session, latest)
    assert decision is not None and (decision.actor_id, decision.outcome) == ("alice", "approved")
