"""P6-1: a recorded verdict is signed in the background, pinned to that verdict, and never delays or changes the send."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lemoncrow.core.settings import write_setting
from lemoncrow.pro.capabilities.review.record_signing import USER_KEY_SETTING, generate_user_key
from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship
from lemoncrow.pro.capabilities.review.store import ReviewStore
from tests.gateway.test_review_wrap_feedback import TOKEN, _no_note_review, review  # noqa: F401

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def _send(client: TestClient, review_id: str, revision: str, outcome: str) -> None:
    started = client.post(
        f"/api/reviews/{review_id}/flow-send",
        headers=HEADERS,
        json={"expected_revision_id": revision, "outcome": outcome, "summary": "", "feedback": None},
    )
    assert started.status_code == 200, started.text
    time.sleep(0.13)
    asyncio.run(client.app.state.review_flow_dispatch_due())


def _post_outcome(client: TestClient, review_id: str, revision: str, outcome: str) -> None:
    posted = client.post(
        f"/api/reviews/{review_id}/outcome",
        headers=HEADERS,
        json={"outcome": outcome, "summary": "", "expected_revision_id": revision},
    )
    assert posted.status_code == 200, posted.text


def _records(client: TestClient, review_id: str) -> list[dict[str, Any]]:
    body = client.get(f"/api/reviews/{review_id}/record", headers=HEADERS).json()
    records: list[dict[str, Any]] = body["records"]
    return records


def _wait_records(client: TestClient, review_id: str, count: int, seconds: float = 10.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + seconds
    while True:
        found = _records(client, review_id)
        if len(found) >= count or time.monotonic() > deadline:
            return found
        time.sleep(0.05)


@pytest.fixture
def key_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("LEMONCROW_REVIEW_UNDO_MS", "100")
    return root


def _opt_in(root: Path) -> str:
    key_path = root / "review-signing" / "user-key.pem"
    key_id = generate_user_key(key_path)
    write_setting(root, USER_KEY_SETTING, str(key_path))
    return key_id


def test_a_send_with_a_key_signs_the_verdict_it_recorded(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    client, review_id, revision = _no_note_review(review)
    key_id = _opt_in(key_root)
    _send(client, review_id, revision, "lgtm")
    assert client.get(f"/api/reviews/{review_id}/flow-send", headers=HEADERS).json()["state"] == "sent"
    [record] = _wait_records(client, review_id, 1)
    assert (record["signing_state"], record["key_id"], record["decision"]) == ("signed", key_id, "approved")
    assert record["decision_kind"] == "human_outcome" and record["parts_reviewed_one_by_one"] is not None
    envelope = client.get(f"/api/reviews/{review_id}/records/{record['record_id']}", headers=HEADERS).json()
    statement = json.loads(base64.b64decode(envelope["payload"]))
    assert statement["predicate"]["snapshot"]["revision_id"] == revision
    verified = client.get(f"/api/reviews/{review_id}/records/{record['record_id']}/verify", headers=HEADERS).json()
    assert verified["outcome"]["outcome"] == "valid"


def test_a_verdict_recorded_by_the_outcome_route_is_signed_too(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    """Welcome back's Approve posts the verdict directly, with no send."""

    client, review_id, revision = _no_note_review(review)
    _opt_in(key_root)
    _post_outcome(client, review_id, revision, "lgtm")
    [record] = _wait_records(client, review_id, 1)
    assert (record["decision_kind"], record["decision"]) == ("human_outcome", "approved")


def test_a_send_without_a_key_still_sends_and_signs_nothing(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    client, review_id, revision = _no_note_review(review)
    _send(client, review_id, revision, "lgtm")
    assert client.get(f"/api/reviews/{review_id}/flow-send", headers=HEADERS).json()["state"] == "sent"
    time.sleep(0.3)
    assert _records(client, review_id) == []
    assert client.get(f"/api/reviews/{review_id}/outcome", headers=HEADERS).json()["current"]["outcome"] == "lgtm"


def test_a_signer_that_raises_never_changes_the_send(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    client, review_id, revision = _no_note_review(review)

    def broken(_review_id: str, _outcome: Any) -> dict[str, Any]:
        raise RuntimeError("the key provider fell over")

    client.app.state.review_record_signer_at_send = broken  # type: ignore[attr-defined]
    _send(client, review_id, revision, "lgtm")
    assert client.get(f"/api/reviews/{review_id}/flow-send", headers=HEADERS).json()["state"] == "sent"
    assert client.get(f"/api/reviews/{review_id}/finish", headers=HEADERS).json()["status"] == "finished"


def test_a_slow_signer_does_not_delay_the_send(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    client, review_id, revision = _no_note_review(review)
    release = threading.Event()
    entered = threading.Event()

    def slow(_review_id: str, _outcome: Any) -> dict[str, Any]:
        entered.set()
        release.wait(15)
        return {"status": "not_signed", "code": "slow"}

    client.app.state.review_record_signer_at_send = slow  # type: ignore[attr-defined]
    try:
        started = time.monotonic()
        _send(client, review_id, revision, "lgtm")
        took = time.monotonic() - started
        assert entered.wait(5), "the signer was never started"
        # The whole send (verdict, finish, state) finished while the signer was still blocked.
        assert client.get(f"/api/reviews/{review_id}/flow-send", headers=HEADERS).json()["state"] == "sent"
        assert not release.is_set() and took < 5
    finally:
        release.set()


def test_a_comment_signs_nothing(review: tuple[TestClient, str, str, str], key_root: Path) -> None:  # noqa: F811
    client, review_id, revision = _no_note_review(review)
    _opt_in(key_root)
    _send(client, review_id, revision, "comment")
    assert client.get(f"/api/reviews/{review_id}/flow-send", headers=HEADERS).json()["state"] == "sent"
    time.sleep(0.5)
    assert _records(client, review_id) == []


def test_a_comment_after_an_approval_does_not_sign_the_old_approval_again(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    client, review_id, revision = _no_note_review(review)
    _opt_in(key_root)
    _post_outcome(client, review_id, revision, "lgtm")
    [first] = _wait_records(client, review_id, 1)
    _post_outcome(client, review_id, revision, "comment")
    time.sleep(0.5)
    assert [row["record_id"] for row in _records(client, review_id)] == [first["record_id"]]


def test_a_merged_landing_on_the_revision_does_not_turn_a_persons_verdict_into_a_merge_record(
    review: tuple[TestClient, str, str, str], key_root: Path, tmp_path: Path  # noqa: F811
) -> None:
    client, review_id, revision = _no_note_review(review)
    _opt_in(key_root)
    store = ReviewStore(tmp_path / "store")
    store.upsert_relationship(
        ReviewRelationship(
            id="",
            review_id=review_id,
            kind="landing",
            relation="merge_when_ready",
            provider="github",
            external_id="pr-1",
            title="merged",
            status="merged",
            source="lemoncrow",
            metadata_json=json.dumps({"revision_id": revision, "mechanism": "squash"}),
        )
    )
    status = client.get(f"/api/reviews/{review_id}/record", headers=HEADERS).json()
    assert status["decision"]["kind"] == "provider_merge", "the merge is the Review's newest decision"
    _post_outcome(client, review_id, revision, "lgtm")
    kinds = [row["decision_kind"] for row in _wait_records(client, review_id, 1)]
    assert kinds == ["human_outcome"]


def test_the_same_verdict_is_never_signed_twice(
    review: tuple[TestClient, str, str, str], key_root: Path, tmp_path: Path  # noqa: F811
) -> None:
    client, review_id, revision = _no_note_review(review)
    _opt_in(key_root)
    _post_outcome(client, review_id, revision, "lgtm")
    [first] = _wait_records(client, review_id, 1)
    outcome = ReviewStore(tmp_path / "store").list_outcomes(review_id)[0]
    time.sleep(0.2)
    again = client.app.state.review_record_signer_at_send(review_id, outcome)
    assert again == {"status": "already_recorded"}
    assert [row["record_id"] for row in _records(client, review_id)] == [first["record_id"]]


def test_a_capture_landing_after_the_verdict_is_signed_against_nothing(
    review: tuple[TestClient, str, str, str], key_root: Path, tmp_path: Path  # noqa: F811
) -> None:
    """The verdict was about Revision 1; by the time signing runs there is a Revision 2, so no record is made."""

    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
    from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
    from lemoncrow.pro.capabilities.review.sources.local import snapshot_revision

    client, review_id, revision = _no_note_review(review)
    _post_outcome(client, review_id, revision, "lgtm")  # no key yet: stored, not signed
    store = ReviewStore(tmp_path / "store")
    [outcome] = store.list_outcomes(review_id)
    _opt_in(key_root)
    repo_root = tmp_path / "repo"
    (repo_root / "charge.py").write_text("def charge():\n    return 3\n", encoding="utf-8")
    rng = resolve_rev_range(repo_root, working_tree=True)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=tmp_path / "store", with_patch_text=True)
    session = store.get_session(review_id)
    assert session is not None
    snapshot_revision(store, session, repo_root, rng, store_root=tmp_path / "store", build=build)
    assert store.latest_revision(review_id).id != revision
    result = client.app.state.review_record_signer_at_send(review_id, outcome)
    assert result == {"status": "not_signed", "code": "revision_moved"}
    assert _records(client, review_id) == []


def test_export_record_refuses_inside_the_same_read_that_names_the_revision() -> None:
    """The latest-revision read that picks the code is the one compared with the pinned id: no gap between them."""

    from types import SimpleNamespace

    from lemoncrow.pro.capabilities.review.review_record import ReviewDecision
    from lemoncrow.pro.capabilities.review.review_record_api import RevisionMoved, export_record

    reads: list[str] = []
    ids = iter(["rev-2", "rev-3"])

    def latest(_session: Any) -> Any:
        reads.append("latest")
        return SimpleNamespace(id=next(ids))

    def never_signer(_session: Any) -> Any:
        raise AssertionError("the signer must not be reached once the revision has moved")

    deps = SimpleNamespace(latest=latest, authority=SimpleNamespace(signer=never_signer))
    decision = ReviewDecision(
        kind="human_outcome",
        outcome="approved",
        actor_id="local",
        actor_type="human",
        decided_at="2026-11-07T09:00:00+00:00",
    )
    with pytest.raises(RevisionMoved):
        export_record(deps, SimpleNamespace(id="r"), decision=decision, expected_revision_id="rev-1")  # type: ignore[arg-type]
    assert reads == ["latest"], "exactly one read decided both the comparison and the record"


def test_the_record_status_says_a_signature_is_due_only_while_the_server_is_signing_it(
    review: tuple[TestClient, str, str, str], key_root: Path  # noqa: F811
) -> None:
    client, review_id, revision = _no_note_review(review)
    release = threading.Event()
    entered = threading.Event()

    def slow(_review_id: str, _outcome: Any) -> dict[str, Any]:
        entered.set()
        release.wait(15)
        return {"status": "not_signed", "code": "slow"}

    def due() -> bool:
        return bool(client.get(f"/api/reviews/{review_id}/record", headers=HEADERS).json()["signing_due"])

    _post_outcome(client, review_id, revision, "lgtm")  # no key: the real signer declines at once
    time.sleep(0.3)
    assert due() is False
    client.app.state.review_record_signer_at_send = slow  # type: ignore[attr-defined]
    try:
        _post_outcome(client, review_id, revision, "changes_requested")
        assert entered.wait(5)
        assert due() is True, "the server is signing this verdict right now"
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while due() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert due() is False
