"""P7-1: the second reviewer routes, with a fake multi-person authority (hosted supplies the real one)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pygit2
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review import viewer_lens
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.record_signing import (
    Ed25519RecordSigner,
    SigningStatus,
    key_set_document,
    public_key_entry,
)
from lemoncrow.pro.capabilities.review.second_reviewer import OwnerRef
from lemoncrow.pro.capabilities.review.second_reviewer_api import ASK_KIND
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore

TOKEN = "second-reviewer-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SAM, MAYA, DANA = "usr_sam", "usr_maya", "usr_dana"
PEOPLE = {
    SAM: OwnerRef(SAM, "srivera", "Sam Rivera"),
    MAYA: OwnerRef(MAYA, "mchen", "Maya Chen"),
    DANA: OwnerRef(DANA, "dkim", "Dana Kim"),
}


class FakeOrgSigning:
    """An organization key held in this process (hosted holds it in KMS)."""

    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.key_id = "org-key-1"
        self.available = True
        self.mode = "organization_key"
        self.exporter_of: Any = None  # a test that retracts signs as the person (a retraction is signed by its owner)

    def status(self, session: ReviewSession) -> SigningStatus:
        return SigningStatus(mode=self.mode, available=self.available, key_id=self.key_id, algorithm="Ed25519")

    def signer(self, session: ReviewSession) -> Ed25519RecordSigner:
        return Ed25519RecordSigner(
            self.key, key_id=self.key_id, mode="organization_key", issuer_kind="organization", issuer_id="acme"
        )

    def public_keys(self, session: ReviewSession) -> Mapping[str, Any]:
        return key_set_document(
            [public_key_entry(self.key.public_key(), key_id=self.key_id, issuer="acme")], issuer="acme"
        )

    def exporter(self, session: ReviewSession) -> tuple[str, str]:
        return (self.exporter_of(), "human") if self.exporter_of else ("control-plane", "system")

    def issuer(self, session: ReviewSession) -> tuple[str, str]:
        return "organization", "acme"

    def audit(self, session: ReviewSession, event: str, *, allowed: bool, detail: Mapping[str, str]) -> None:
        return None


class FakeAuthority:
    def __init__(self, signing: FakeOrgSigning, who: dict[str, str]) -> None:
        self.signing = signing
        self.who = who
        self.app: FastAPI | None = None
        self.store: ReviewStore | None = None
        self.repo: Any = None
        self.owner_paths: dict[str, list[OwnerRef]] = {"link.txt": [PEOPLE[MAYA]]}
        self.author = "usr_author"

    def viewer_id(self) -> str:
        return self.who["id"]

    def barred_ids(self, session: ReviewSession) -> Sequence[str]:
        return (self.author,)

    def owners(
        self, session: ReviewSession, revision: ReviewRevision, paths: Sequence[str]
    ) -> Mapping[str, Sequence[OwnerRef]]:
        return {path: self.owner_paths.get(path, []) for path in paths}

    def person(self, session: ReviewSession, principal_id: str) -> OwnerRef:
        return PEOPLE.get(principal_id, OwnerRef(principal_id))

    def signing_unavailable(self, session: ReviewSession) -> str:
        return "" if self.signing.available else "Records are not signed for this organization."

    def sign(self, review_id: str, outcome_id: str, detail: Mapping[str, str]) -> Mapping[str, Any]:
        assert self.app is not None
        signer = self.app.state.review_sign_pinned_outcome
        return dict(signer(review_id, outcome_id, detail))


@pytest.fixture
def team(tmp_path: Path) -> tuple[TestClient, str, str, dict[str, str], FakeAuthority]:
    root = tmp_path / "repo"
    root.mkdir()
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Second Reviewer"
    repo.config["user.email"] = "second@example.invalid"
    (root / "charge.py").write_text("def charge(user, amount):\n    return amount\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("Second Reviewer", "second@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", sig, sig, "baseline", tree, [])
    # A new symlink is a decision whatever anyone marks (answer_category): the area that needs two people.
    (root / "link.txt").symlink_to("charge.py")
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(root, working_tree=True)
    built = build_review_packet_with_blobs(root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, root, rng, title=built.packet.title)
    first = snapshot_revision(store, session, root, rng, store_root=store_root, build=built)
    store.publish_snapshot(session.id, first.id)
    who = {"id": SAM}
    signing = FakeOrgSigning()
    authority = FakeAuthority(signing, who)
    app = FastAPI()
    authority.app = app
    authority.store = store
    authority.repo = (root, store_root, session)
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=root,
        port=48080,
        acting_reviewer_id=lambda: who["id"],
        record_signing=signing,
        second_reviewer=authority,
    )
    client = TestClient(app, base_url="http://127.0.0.1:48080")
    return client, session.id, first.id, who, authority


def _status(client: TestClient, review_id: str) -> dict[str, Any]:
    response = client.get(f"/api/reviews/{review_id}/second-reviewer", headers=HEADERS)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _verdict(client: TestClient, review_id: str, revision: str, outcome: str = "lgtm") -> None:
    posted = client.post(
        f"/api/reviews/{review_id}/outcome",
        headers=HEADERS,
        json={"outcome": outcome, "summary": "", "expected_revision_id": revision},
    )
    assert posted.status_code == 200, posted.text


def _unit(client: TestClient, review_id: str) -> str:
    return str(client.get(f"/api/reviews/{review_id}/targets", headers=HEADERS).json()["targets"][0]["unit_key"])


def _revision_of(client: TestClient, review_id: str) -> str:
    return str(client.get(f"/api/reviews/{review_id}/revisions", headers=HEADERS).json()["revisions"][-1]["id"])


def _mark(client: TestClient, review_id: str, unit: str, state: str) -> None:
    revision = _revision_of(client, review_id)
    response = client.post(
        f"/api/reviews/{review_id}/marks",
        headers=HEADERS,
        json={"unit_key": unit, "state": state, "expected_revision_id": revision},
    )
    assert response.status_code == 200, response.text


def test_the_one_change_in_the_fixture_is_a_decision_with_a_codeowner(
    team: tuple[TestClient, str, str, dict[str, str], FakeAuthority],
) -> None:
    client, review_id, _revision, _who, _authority = team
    status = _status(client, review_id)
    assert (status["state"], status["role"], status["required"]) == ("will_be_asked", "first", True)
    assert status["reviewer"]["name"] == "Maya Chen"
    assert status["signatures"] == {"have": 0, "needed": 2}


Team = tuple[TestClient, str, str, dict[str, str], FakeAuthority]


def _as(who: dict[str, str], principal: str) -> None:
    who["id"] = principal


def _sam_answers_and_approves(team: Team, *, note: str = "Sam's private reasoning about the symlink") -> str:
    client, review_id, revision, who, _authority = team
    _as(who, SAM)
    unit = _unit(client, review_id)
    _mark(client, review_id, unit, "needs_changes")
    noted = client.post(
        f"/api/reviews/{review_id}/annotations",
        headers=HEADERS,
        json={
            "path": "link.txt",
            "start_line": 0,
            "end_line": 0,
            "file_level": True,
            "body": note,
            "kind": "request_change",
            "target_unit_key": unit,
            "expected_revision_id": revision,
        },
    )
    assert noted.status_code in {200, 201}, noted.text
    _verdict(client, review_id, revision)
    return unit


def test_nobody_is_asked_before_the_first_reviewer_approves(team: Team) -> None:
    client, review_id, revision, who, _authority = team
    _as(who, SAM)
    assert _status(client, review_id)["state"] == "will_be_asked"
    _verdict(client, review_id, revision, "changes_requested")
    status = _status(client, review_id)
    assert (status["state"], status["verdict"]) == ("not_needed_yet", "changes_requested")


def test_approving_asks_the_codeowner_and_signs_the_first_signature(team: Team) -> None:
    client, review_id, _revision, who, _authority = team
    _sam_answers_and_approves(team)
    status = _status(client, review_id)
    assert (status["state"], status["role"], status["verdict"]) == ("waiting", "first", "waiting")
    assert status["reviewer"]["name"] == "Maya Chen"
    assert status["first"]["signed"] is True
    # "1 of 2" is one signature that exists and verifies, never a promise
    assert status["signatures"] == {"have": 1, "needed": 2}
    assert status["second"] is None
    _as(who, MAYA)
    assert _status(client, review_id)["role"] == "second"


def test_the_second_reviewer_cannot_see_the_first_reviewers_answers_until_she_answers(team: Team) -> None:
    client, review_id, _revision, who, _authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    hidden = client.get(f"/api/reviews/{review_id}/second-reviewer", headers=HEADERS)
    body = hidden.json()
    assert body["first_answers_hidden"] is True and body["answered"] is False
    assert "first_answers" not in body
    assert "Sam's private reasoning" not in hidden.text
    # nothing she can read leaks them either: her own marks are hers, and the whole payload has no copy of his
    assert [part["state"] for part in body["parts"]] == ["unreviewed"]

    _mark(client, review_id, unit, "reviewed")
    shown = client.get(f"/api/reviews/{review_id}/second-reviewer", headers=HEADERS).json()
    assert shown["answered"] is True and shown["first_answers_hidden"] is False
    [theirs] = shown["first_answers"]
    assert theirs["state"] == "needs_changes" and theirs["notes"] == ["Sam's private reasoning about the symlink"]
    assert shown["parts"][0]["state"] == "reviewed"


def test_signing_in_agreement_gives_two_of_two_and_both_signatures_verify(team: Team) -> None:
    client, review_id, revision, who, _authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    signed = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert signed.status_code == 200, signed.text
    body = signed.json()
    assert (body["state"], body["verdict"]) == ("agrees", "approved")
    assert body["signatures"] == {"have": 2, "needed": 2}
    assert body["second"]["signed"] is True and body["second"]["outcome"] == "lgtm"
    _as(who, SAM)
    records = client.get(f"/api/reviews/{review_id}/record", headers=HEADERS).json()["records"]
    assert len(records) == 2
    ids = {record["record_id"] for record in records}
    assert {body["first"]["record_id"], body["second"]["record_id"]} == ids
    for record_id in ids:
        verified = client.get(f"/api/reviews/{review_id}/records/{record_id}/verify", headers=HEADERS).json()
        assert verified["outcome"]["outcome"] == "valid"
    # a second signature is bound to the approval it co-signs
    import base64
    import json

    envelope = client.get(f"/api/reviews/{review_id}/records/{body['second']['record_id']}", headers=HEADERS).json()
    detail = json.loads(base64.b64decode(envelope["payload"]))["predicate"]["decision"]["detail"]
    assert detail == {"second_signature_of": body["first"]["outcome_id"]}


def test_signing_a_disagreement_blocks_approval_and_needs_a_note(team: Team) -> None:
    client, review_id, revision, who, _authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "needs_changes")
    url = f"/api/reviews/{review_id}/second-reviewer/sign"
    assert client.post(url, headers=HEADERS, json={"expected_revision_id": revision}).status_code == 422
    signed = client.post(
        url, headers=HEADERS, json={"expected_revision_id": revision, "note": "Do not follow symlinks here."}
    )
    body = signed.json()
    assert (body["state"], body["verdict"]) == ("disagrees", "changes_requested")
    assert body["second"]["outcome"] == "changes_requested" and body["second"]["note"] == "Do not follow symlinks here."
    # a disagreement is a signature that exists too
    assert body["signatures"] == {"have": 2, "needed": 2}


@pytest.mark.parametrize(
    ("principal", "answered_first", "status"),
    [(SAM, False, 403), (DANA, False, 403), (MAYA, False, 409)],
)
def test_only_the_asked_second_reviewer_signs_and_only_after_answering(
    team: Team, principal: str, answered_first: bool, status: int
) -> None:
    client, review_id, revision, who, _authority = team
    _sam_answers_and_approves(team)
    _as(who, principal)
    refused = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert refused.status_code == status, refused.text
    _as(who, MAYA)
    assert _status(client, review_id)["state"] == "waiting"


def test_a_second_signature_cannot_be_given_twice(team: Team) -> None:
    client, review_id, revision, who, _authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    url = f"/api/reviews/{review_id}/second-reviewer/sign"
    assert client.post(url, headers=HEADERS, json={"expected_revision_id": revision}).status_code == 200
    assert client.post(url, headers=HEADERS, json={"expected_revision_id": revision}).status_code in {403, 409}


def test_a_first_reviewer_who_takes_the_approval_back_leaves_nothing_to_sign(team: Team) -> None:
    client, review_id, revision, who, _authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, SAM)
    _verdict(client, review_id, revision, "changes_requested")
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    refused = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert refused.status_code in {403, 409}
    assert _status(client, review_id)["state"] != "waiting"


def test_without_signing_nobody_is_asked_and_no_count_is_made_up(team: Team) -> None:
    client, review_id, revision, who, authority = team
    authority.signing.available = False
    _as(who, SAM)
    _verdict(client, review_id, revision)
    status = _status(client, review_id)
    assert (status["state"], status["required"], status["signatures"]) == (
        "unavailable",
        False,
        {"have": 0, "needed": 1},
    )
    assert "not signed" in status["unavailable"]


def test_the_author_is_never_the_second_reviewer(team: Team) -> None:
    client, review_id, _revision, who, authority = team
    authority.owner_paths = {"link.txt": [OwnerRef(authority.author, "author", "The Author")]}
    _as(who, SAM)
    status = _status(client, review_id)
    assert (status["state"], status["required"]) == ("unavailable", False)
    assert "no CODEOWNER" in status["unavailable"]


def test_a_local_review_has_no_second_reviewer_route_or_field(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    repo = pygit2.init_repository(str(root), initial_head="main")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("L", "l@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", sig, sig, "base", tree, [])
    (root / "a.py").write_text("x = 2\n", encoding="utf-8")
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(root, working_tree=True)
    built = build_review_packet_with_blobs(root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, root, rng, title=built.packet.title)
    first = snapshot_revision(store, session, root, rng, store_root=store_root, build=built)
    store.publish_snapshot(session.id, first.id)
    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency(TOKEN), repo_root=root, port=48080)
    client = TestClient(app, base_url="http://127.0.0.1:48080")
    assert client.get(f"/api/reviews/{session.id}/second-reviewer", headers=HEADERS).status_code == 404
    assert client.post(f"/api/reviews/{session.id}/second-reviewer/sign", headers=HEADERS, json={}).status_code == 404
    assert not hasattr(app.state, "review_second_reviewer_on_verdict")
    assert "second" not in client.get(f"/api/reviews/{session.id}/outcome", headers=HEADERS).text
    posted = client.post(
        f"/api/reviews/{session.id}/outcome",
        headers=HEADERS,
        json={"outcome": "lgtm", "summary": "", "expected_revision_id": first.id},
    )
    assert posted.status_code == 200 and "second" not in posted.text
    assert store.list_relationships(session.id, kind=ASK_KIND) == ()


def _review_status(client: TestClient, review_id: str) -> str:
    return str(client.get(f"/api/reviews/{review_id}", headers=HEADERS).json()["session"]["status"])


def _finish(client: TestClient, review_id: str, revision: str) -> None:
    done = client.post(
        f"/api/reviews/{review_id}/finish",
        headers=HEADERS,
        json={"status": "finished", "expected_revision_id": revision},
    )
    assert done.status_code == 200, done.text


def test_the_review_stays_open_until_the_second_signature_and_finishes_when_both_agree(team: Team) -> None:
    client, review_id, revision, who, _authority = team
    unit = _sam_answers_and_approves(team)
    _finish(client, review_id, revision)  # Wrap up finishes after the verdict; R3: not while a signature is owed
    assert _review_status(client, review_id) == "open"
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    signed = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert signed.status_code == 200, signed.text
    assert _review_status(client, review_id) == "finished"


def test_a_disagreement_leaves_the_review_open(team: Team) -> None:
    client, review_id, revision, who, _authority = team
    unit = _sam_answers_and_approves(team)
    _finish(client, review_id, revision)
    _as(who, MAYA)
    _mark(client, review_id, unit, "needs_changes")
    client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign",
        headers=HEADERS,
        json={"expected_revision_id": revision, "note": "Needs a guard."},
    )
    assert _review_status(client, review_id) == "open"


def test_with_no_second_reviewer_required_finishing_still_finishes(team: Team) -> None:
    client, review_id, revision, who, authority = team
    authority.owner_paths = {}
    _as(who, SAM)
    _verdict(client, review_id, revision)
    _finish(client, review_id, revision)
    assert _review_status(client, review_id) == "finished"


def test_the_check_run_counts_only_signatures_that_exist_and_verify(team: Team) -> None:
    from lemoncrow.pro.capabilities.review.github_publish import check_run_for

    client, review_id, revision, who, authority = team
    assert authority.store is not None
    session = authority.store.get_session(review_id)
    signal_of = client.app.state.review_signature_signal

    def run(event: str = "APPROVE") -> dict[str, Any] | None:
        return check_run_for(head_sha="b52e7a9" + "0" * 33, event=event, signal=signal_of(session))

    _as(who, SAM)
    assert run() is None  # nobody has signed: no count, no check run
    unit = _sam_answers_and_approves(team)
    waiting = run()
    assert waiting is not None
    assert (
        waiting["text"].endswith("1 of 2 signatures")
        and waiting["label"] == "Pending \u00b7 waiting for a second signature"
    )
    assert "signed by @srivera" in waiting["text"]
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    _as(who, SAM)
    done = run()
    assert done is not None and done["label"] == "Passing" and done["text"].endswith("2 of 2 signatures")


def test_a_disagreement_fails_the_check_run(team: Team) -> None:
    from lemoncrow.pro.capabilities.review.github_publish import check_run_for

    client, review_id, revision, who, authority = team
    assert authority.store is not None
    session = authority.store.get_session(review_id)
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "needs_changes")
    client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign",
        headers=HEADERS,
        json={"expected_revision_id": revision, "note": "No."},
    )
    run = check_run_for(head_sha="a" * 40, event="APPROVE", signal=client.app.state.review_signature_signal(session))
    assert run is not None and run["state"] == "failing"


def _verify(client: TestClient, review_id: str, record_id: str) -> dict[str, Any]:
    body: dict[str, Any] = client.get(f"/api/reviews/{review_id}/records/{record_id}/verify", headers=HEADERS).json()
    return body


def _row(body: dict[str, Any], label: str) -> str | None:
    return next((row["value"] for row in body["details"] if row["label"] == label), None)


def test_the_record_says_who_signed_second_only_when_that_signature_exists(team: Team) -> None:
    client, review_id, revision, who, _authority = team
    unit = _sam_answers_and_approves(team)
    first = _status(client, review_id)["first"]["record_id"]
    waiting = _verify(client, review_id, first)
    assert _row(waiting, "Second signature") == "Waiting · Maya Chen"
    assert waiting["second_signature"]["state"] == "waiting"
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    _as(who, SAM)
    signed = _verify(client, review_id, first)
    value = _row(signed, "Second signature")
    assert value is not None and value.startswith("Maya Chen · @mchen · ") and value.endswith(" UTC · agrees")
    assert signed["second_signature"]["state"] == "agrees"
    hers = _status(client, review_id)["second"]["record_id"]
    assert _row(_verify(client, review_id, hers), "Second signature of") == "co-signs the approval by Sam Rivera"


def test_a_request_for_changes_record_says_the_second_signature_was_not_needed(team: Team) -> None:
    client, review_id, revision, who, _authority = team
    _as(who, SAM)
    _verdict(client, review_id, revision, "changes_requested")
    authority = team[4]
    # signing a request-for-changes record: the hosted hook signs only approvals, so sign it as the organization here
    outcome_id = client.get(f"/api/reviews/{review_id}/outcome", headers=HEADERS).json()["current"]["id"]
    signed = authority.sign(review_id, outcome_id, {})
    assert signed["status"] == "recorded"
    record_id = client.get(f"/api/reviews/{review_id}/record", headers=HEADERS).json()["records"][0]["record_id"]
    assert _row(_verify(client, review_id, record_id), "Second signature") == "Not needed · changes were requested"


def test_a_local_record_has_no_second_signature_row_or_field(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    repo = pygit2.init_repository(str(root), initial_head="main")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    sig = pygit2.Signature("L", "l@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", sig, sig, "base", repo.index.write_tree(), [])
    (root / "a.py").write_text("x = 2\n", encoding="utf-8")
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(root, working_tree=True)
    built = build_review_packet_with_blobs(root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, root, rng, title=built.packet.title)
    first = snapshot_revision(store, session, root, rng, store_root=store_root, build=built)
    store.publish_snapshot(session.id, first.id)
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=root,
        port=48080,
        record_signing=FakeOrgSigning(),
    )
    client = TestClient(app, base_url="http://127.0.0.1:48080")
    _verdict(client, session.id, first.id)
    exported = client.post(f"/api/reviews/{session.id}/record", headers=HEADERS, json={})
    assert exported.status_code == 201, exported.text
    record_id = client.get(f"/api/reviews/{session.id}/record", headers=HEADERS).json()["records"][0]["record_id"]
    body = _verify(client, session.id, record_id)
    assert "second_signature" not in body
    assert not any("econd" in row["label"] for row in body["details"])


def _notes(client: TestClient, review_id: str) -> list[str]:
    body = client.get(f"/api/reviews/{review_id}/annotations", headers=HEADERS).json()
    return [str(note["body"]) for note in body["annotations"]]


def test_the_first_reviewers_notes_are_withheld_from_the_second_until_she_answers(team: Team) -> None:
    client, review_id, _revision, who, _authority = team
    unit = _sam_answers_and_approves(team, note="Sam's private reasoning")
    _as(who, SAM)
    assert "Sam's private reasoning" in _notes(client, review_id)  # his own notes are his
    _as(who, MAYA)
    assert "Sam's private reasoning" not in _notes(client, review_id)
    assert (
        "Sam's private reasoning"
        not in client.get(f"/api/reviews/{review_id}/revisions/0/annotations", headers=HEADERS).text
    )
    _mark(client, review_id, unit, "reviewed")
    assert "Sam's private reasoning" in _notes(client, review_id)


def test_someone_who_was_never_asked_still_sees_the_notes(team: Team) -> None:
    client, review_id, _revision, who, _authority = team
    _sam_answers_and_approves(team, note="Sam's private reasoning")
    _as(who, DANA)
    assert "Sam's private reasoning" in _notes(client, review_id)


# --------------------------------------------------------------------------- review round 1


def _condition(client: TestClient, review_id: str, name: str = "human-approval-state") -> dict[str, Any]:
    body = client.get(f"/api/reviews/{review_id}/readiness", headers=HEADERS).json()
    return next(item for item in body["conditions"] if item["id"] == name)


def _asks(authority: FakeAuthority, review_id: str) -> list[tuple[str, str]]:
    from lemoncrow.pro.capabilities.review.second_reviewer_api import list_asks

    assert authority.store is not None
    return [(ask.second_reviewer_id, ask.state) for ask in list_asks(authority.store, review_id)]


def _sams_ask(authority: FakeAuthority, review_id: str) -> str:
    from lemoncrow.pro.capabilities.review.second_reviewer_api import list_asks

    assert authority.store is not None
    return next(ask.state for ask in list_asks(authority.store, review_id) if ask.first_reviewer_id == SAM)


def test_a_third_persons_verdict_does_not_withdraw_the_standing_ask(team: Team) -> None:
    client, review_id, revision, who, authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, DANA)
    _verdict(client, review_id, revision, "comment")
    assert _sams_ask(authority, review_id) == "waiting"
    _verdict(client, review_id, revision, "lgtm")  # even another person's own approval
    assert _sams_ask(authority, review_id) == "waiting"
    _as(who, SAM)
    status = _status(client, review_id)
    assert (status["state"], status["signatures"]) == ("waiting", {"have": 1, "needed": 2})
    _finish(client, review_id, revision)
    assert _review_status(client, review_id) == "open"
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    signed = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert signed.status_code == 200, signed.text


def test_the_first_reviewer_commenting_after_approving_keeps_the_ask(team: Team) -> None:
    client, review_id, revision, who, authority = team
    _sam_answers_and_approves(team)
    _as(who, SAM)
    _verdict(client, review_id, revision, "comment")
    assert _asks(authority, review_id)[0] == (MAYA, "waiting")


def test_waiting_is_reported_only_while_a_waiting_ask_stands_and_the_finish_gate_agrees(team: Team) -> None:
    client, review_id, revision, who, authority = team
    _sam_answers_and_approves(team)
    _as(who, SAM)
    assert (_status(client, review_id)["state"], _asks(authority, review_id)[0][1]) == ("waiting", "waiting")
    # Sam takes the approval back: the ask is withdrawn by the write, and nothing reports waiting any more.
    _verdict(client, review_id, revision, "changes_requested")
    assert _asks(authority, review_id)[0][1] == "withdrawn"
    assert _status(client, review_id)["state"] == "not_needed_yet"
    _finish(client, review_id, revision)
    assert _review_status(client, review_id) == "finished"


def test_a_status_read_never_writes(team: Team) -> None:
    client, review_id, revision, who, authority = team
    _sam_answers_and_approves(team)
    assert authority.store is not None
    # Sam's approval is replaced behind the hook's back (a direct store write): a GET must not withdraw anything.
    authority.store.record_outcome(review_id, revision, SAM, outcome="changes_requested")
    before = [
        row.metadata_json + row.status for row in authority.store.list_relationships(review_id, kind="second_signature")
    ]
    _as(who, SAM)
    for _ in range(2):
        shown = _status(client, review_id)
        assert shown["state"] != "waiting"
    after = [
        row.metadata_json + row.status for row in authority.store.list_relationships(review_id, kind="second_signature")
    ]
    assert before == after


def test_a_signed_ask_for_a_superseded_approval_does_not_report_two_of_two(team: Team) -> None:
    client, review_id, revision, who, authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert authority.store is not None
    authority.store.record_outcome(review_id, revision, SAM, outcome="changes_requested")
    _as(who, SAM)
    shown = _status(client, review_id)
    assert shown["state"] != "agrees" and shown["signatures"]["have"] < shown["signatures"]["needed"]


def test_signing_that_cannot_be_given_is_refused_and_leaves_the_ask_and_the_review(team: Team) -> None:
    client, review_id, revision, who, authority = team
    unit = _sam_answers_and_approves(team)
    _finish(client, review_id, revision)
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    authority.signing.available = False
    refused = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert refused.status_code == 503, refused.text
    assert _asks(authority, review_id)[0] == (MAYA, "waiting")
    assert _review_status(client, review_id) == "open"
    assert authority.store is not None and authority.store.list_outcomes(review_id, reviewer_id=MAYA) == ()
    authority.signing.available = True
    again = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert again.status_code == 200 and again.json()["signatures"] == {"have": 2, "needed": 2}


def test_a_signature_that_does_not_verify_gives_nothing_and_finishes_nothing(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, review_id, revision, who, authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    real = authority.sign
    monkeypatch.setattr(authority, "sign", lambda *args: {"status": "not_signed", "code": "forbidden"})
    refused = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert refused.status_code == 503
    assert _asks(authority, review_id)[0] == (MAYA, "waiting")
    _as(who, SAM)
    assert _review_status(client, review_id) == "open"
    shown = _status(client, review_id)
    assert (shown["state"], shown["signatures"]) == ("waiting", {"have": 1, "needed": 2})
    monkeypatch.setattr(authority, "sign", real)


def test_a_concurrent_sign_leaves_no_orphan_verdict(team: Team) -> None:
    from lemoncrow.pro.capabilities.review.second_reviewer_api import claim_ask, list_asks

    client, review_id, revision, who, authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    assert authority.store is not None
    claimed = claim_ask(authority.store, list_asks(authority.store, review_id)[0])  # another request is signing
    assert claimed is not None
    refused = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert refused.status_code == 409
    assert authority.store.list_outcomes(review_id, reviewer_id=MAYA) == ()


def test_only_both_signatures_make_the_approval_count_in_readiness(team: Team) -> None:
    client, review_id, revision, who, _authority = team
    unit = _sam_answers_and_approves(team)
    waiting = _condition(client, review_id)
    assert (waiting["state"], waiting["reason"]) == ("pending", "second_signature_pending")
    _as(who, MAYA)
    _verdict(client, review_id, revision, "lgtm")  # Maya approving by the ordinary route is not her signature
    assert _condition(client, review_id)["state"] == "pending"
    _mark(client, review_id, unit, "reviewed")
    client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert _condition(client, review_id)["state"] == "pass"


def test_the_ask_follows_the_revision_not_the_person(team: Team) -> None:

    client, review_id, rev1, who, authority = team
    root, store_root, session = authority.repo
    assert authority.store is not None
    authority.owner_paths = {"link.txt": [PEOPLE[SAM]]}
    _as(who, DANA)
    _mark(client, review_id, _unit(client, review_id), "reviewed")
    _verdict(client, review_id, rev1)
    assert _asks(authority, review_id)[0] == (SAM, "waiting")
    # a new revision arrives; Sam is now an ordinary first reviewer of it
    (root / "link2.txt").symlink_to("charge.py")
    rng = resolve_rev_range(root, working_tree=True)
    built = build_review_packet_with_blobs(root, rng, store_root=store_root, with_patch_text=True)
    second = snapshot_revision(authority.store, session, root, rng, store_root=store_root, build=built)
    authority.store.publish_snapshot(review_id, second.id)
    authority.owner_paths = {"link.txt": [PEOPLE[MAYA]], "link2.txt": [PEOPLE[MAYA]]}
    _as(who, SAM)
    for part in client.get(f"/api/reviews/{review_id}/targets", headers=HEADERS).json()["targets"]:
        if part["answer"]["category"] == "decision":
            _mark(client, review_id, part["unit_key"], "reviewed")
    _verdict(client, review_id, second.id)
    states = dict(_asks(authority, review_id))
    assert states[MAYA] == "waiting"
    assert states[SAM] == "withdrawn"


# --------------------------------------------------------------------------- review round 2


def _activity(client: TestClient, review_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = client.get(f"/api/reviews/{review_id}/activity", headers=HEADERS).json()["events"]
    return events


def test_the_activity_feed_hides_what_the_first_reviewer_did_until_the_second_has_answered(team: Team) -> None:
    client, review_id, _revision, who, _authority = team
    unit = _sam_answers_and_approves(team, note="Sam's private reasoning")
    _as(who, SAM)
    mine = _activity(client, review_id)
    assert any(item["actor_id"] == SAM for item in mine)  # his own history is his
    _as(who, MAYA)
    seen = _activity(client, review_id)
    assert not [item for item in seen if item["actor_id"] == SAM]
    text = client.get(f"/api/reviews/{review_id}/activity", headers=HEADERS).text
    assert SAM not in text and "needs_changes" not in text
    _mark(client, review_id, unit, "reviewed")
    assert any(item["actor_id"] == SAM for item in _activity(client, review_id))


def test_the_first_reviewers_notes_do_not_reach_the_second_by_feedback_export_or_comment_history(team: Team) -> None:
    client, review_id, revision, who, _authority = team
    unit = _sam_answers_and_approves(team, note="Sam's private reasoning")
    _as(who, SAM)
    note = client.get(f"/api/reviews/{review_id}/annotations", headers=HEADERS).json()["annotations"][0]["id"]
    assert client.get(f"/api/annotations/{note}/versions", headers=HEADERS).status_code == 200
    _as(who, MAYA)
    assert client.get(f"/api/annotations/{note}/versions", headers=HEADERS).status_code == 404
    export = client.post(
        f"/api/reviews/{review_id}/feedback/export", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert "Sam's private reasoning" not in export.text
    _mark(client, review_id, unit, "reviewed")
    assert client.get(f"/api/annotations/{note}/versions", headers=HEADERS).status_code == 200


def test_a_signature_that_fails_after_the_verdict_was_recorded_leaves_no_vote_and_no_note(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, review_id, revision, who, authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "needs_changes")
    monkeypatch.setattr(authority, "sign", lambda *args: {"status": "not_signed"})
    refused = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign",
        headers=HEADERS,
        json={"expected_revision_id": revision, "note": "Maya's unsigned note"},
    )
    assert refused.status_code == 503
    _as(who, SAM)
    assert _condition(client, review_id)["reason"] == "second_signature_pending"  # not "changes requested"
    shown = client.get(f"/api/reviews/{review_id}/second-reviewer", headers=HEADERS).text
    assert "Maya's unsigned note" not in shown
    assert authority.store is not None
    assert authority.store.latest_outcome(review_id, MAYA) is None  # the failed attempt's verdict is void: she has none


def test_a_slow_signer_that_was_overtaken_does_not_release_the_takeovers_claim(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.second_reviewer_api import claim_ask, list_asks

    client, review_id, revision, who, authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    assert authority.store is not None
    store = authority.store

    def overtaken(*_args: Any) -> dict[str, str]:
        # while this request is "signing" slowly, its claim goes stale and another request takes the ask over
        ask = list_asks(store, review_id)[0]
        meta = json.loads(ask.relationship.metadata_json)
        meta["signing_since"] = "2000-01-01T00:00:00+00:00"
        store.update_relationship_if_unchanged(ask.relationship, status="signing", metadata_json=json.dumps(meta))
        assert claim_ask(store, list_asks(store, review_id)[0]) is not None
        return {"status": "not_signed"}

    monkeypatch.setattr(authority, "sign", overtaken)
    refused = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert refused.status_code == 503
    assert list_asks(store, review_id)[0].state == "signing"  # the takeover's claim is still its own


def test_a_signed_ask_whose_record_stops_verifying_is_owed_again_by_every_rule(team: Team) -> None:
    client, review_id, revision, who, authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert _condition(client, review_id)["state"] == "pass"
    authority.signing.key = Ed25519PrivateKey.generate()  # the published key is no longer the one that signed
    _as(who, SAM)
    assert _condition(client, review_id)["state"] == "pending"
    assert _status(client, review_id)["state"] == "waiting"


def test_a_superseded_ask_no_longer_keeps_the_inbox_row(team: Team) -> None:
    from lemoncrow.pro.capabilities.review.second_reviewer_api import waiting_asks_by_review

    _client, review_id, revision, _who, authority = team
    _sam_answers_and_approves(team)
    assert authority.store is not None
    assert review_id in waiting_asks_by_review(authority.store)
    authority.store.record_outcome(
        review_id, revision, SAM, outcome="changes_requested"
    )  # a write the hook did not see
    assert review_id not in waiting_asks_by_review(authority.store)


def test_the_first_reviewers_signed_record_is_not_opened_to_the_second_until_she_answers(team: Team) -> None:
    client, review_id, _revision, who, _authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, SAM)
    record = _status(client, review_id)["first"]["record_id"]
    assert client.get(f"/api/reviews/{review_id}/records/{record}", headers=HEADERS).status_code == 200
    _as(who, MAYA)
    assert [
        row["record_id"] for row in client.get(f"/api/reviews/{review_id}/record", headers=HEADERS).json()["records"]
    ] == []
    for route in ("", "/verify"):
        assert client.get(f"/api/reviews/{review_id}/records/{record}{route}", headers=HEADERS).status_code == 404
    _mark(client, review_id, unit, "reviewed")
    assert client.get(f"/api/reviews/{review_id}/records/{record}", headers=HEADERS).status_code == 200


def test_a_retracted_approval_withdraws_the_ask_and_stands_for_nothing_in_readiness(team: Team) -> None:
    client, review_id, revision, who, authority = team
    _sam_answers_and_approves(team)
    assert _sams_ask(authority, review_id) == "waiting"
    assert _condition(client, review_id)["reason"] == "second_signature_pending"
    record = _status(client, review_id)["first"]["record_id"]
    # Sam retracts his own approval (a retraction is signed by the person, with their own key)
    authority.signing.mode = "user_key"
    authority.signing.exporter_of = lambda: who["id"]
    retracted = client.post(f"/api/reviews/{review_id}/records/{record}/retract", headers=HEADERS)
    assert retracted.status_code == 201, retracted.text
    authority.signing.mode = "organization_key"
    authority.signing.exporter_of = None
    assert _sams_ask(authority, review_id) == "withdrawn"
    # the approval stands for nothing, and nothing is waited for either
    condition = _condition(client, review_id)
    assert condition["state"] != "pass" and condition["reason"] != "second_signature_pending"
    assert _status(client, review_id)["state"] != "waiting"
    # Maya has nothing to sign
    _as(who, MAYA)
    refused = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert refused.status_code in {403, 409}
    assert _status(client, review_id)["role"] != "second"


def test_an_earlier_real_decision_of_the_asked_person_keeps_counting_after_a_failed_signing(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, review_id, revision, who, authority = team
    unit = _unit(client, review_id)
    _as(who, MAYA)
    _mark(client, review_id, unit, "needs_changes")
    _verdict(client, review_id, revision, "changes_requested")  # Maya's own, real Request changes, before any ask
    _as(who, SAM)
    _sam_answers_and_approves(team)
    _as(who, MAYA)
    # being asked, she answers differently and her signing fails after the verdict was recorded
    _mark(client, review_id, unit, "reviewed")
    monkeypatch.setattr(authority, "sign", lambda *args: {"status": "not_signed"})
    assert (
        client.post(
            f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
        ).status_code
        == 503
    )
    assert authority.store is not None
    latest = authority.store.latest_outcome(review_id, MAYA)
    assert latest is not None and (latest.outcome, latest.summary) == ("changes_requested", "")
    _as(who, SAM)
    # her earlier Request changes still blocks, and is not mistaken for something recorded since the ask
    assert _condition(client, review_id)["state"] == "fail"


def test_a_failure_before_anything_was_recorded_voids_nothing(team: Team, monkeypatch: pytest.MonkeyPatch) -> None:
    client, review_id, revision, who, authority = team
    unit = _unit(client, review_id)
    _as(who, MAYA)
    _verdict(client, review_id, revision, "changes_requested")
    _as(who, SAM)
    _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    assert authority.store is not None
    before = [row.id for row in authority.store.list_outcomes(review_id, reviewer_id=MAYA)]

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("the store went away")

    monkeypatch.setattr(authority.store, "record_outcome", refuse)
    with pytest.raises(RuntimeError):
        client.post(
            f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
        )
    monkeypatch.undo()
    assert [row.id for row in authority.store.list_outcomes(review_id, reviewer_id=MAYA)] == before


def _maya_signs_and_fails(
    client: TestClient,
    review_id: str,
    revision: str,
    authority: FakeAuthority,
    monkeypatch: pytest.MonkeyPatch,
    note: str = "no",
) -> None:
    monkeypatch.setattr(authority, "sign", lambda *args: {"status": "not_signed"})
    refused = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign",
        headers=HEADERS,
        json={"expected_revision_id": revision, "note": note},
    )
    assert refused.status_code == 503


def test_a_failed_sign_never_lets_a_verdict_recorded_since_the_ask_count(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, review_id, revision, who, authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    _verdict(client, review_id, revision, "lgtm")  # an ordinary approval after being asked: held, not her signature
    assert _condition(client, review_id)["reason"] == "second_signature_pending"
    _mark(client, review_id, unit, "needs_changes")
    _maya_signs_and_fails(client, review_id, revision, authority, monkeypatch)
    assert authority.store is not None
    # the copy of her held approval must stay held: R13, nothing counts as approved until both have signed
    shown = _condition(client, review_id)
    assert (shown["state"], shown["reason"]) == ("pending", "second_signature_pending")
    _as(who, SAM)
    _finish(client, review_id, revision)
    assert _review_status(client, review_id) == "open"


def test_a_failed_sign_that_recorded_nothing_new_does_not_wipe_her_real_decision(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, review_id, revision, who, authority = team
    unit = _unit(client, review_id)
    _as(who, MAYA)
    _verdict(client, review_id, revision, "lgtm")  # a real approval from before she was asked
    _as(who, SAM)
    _sam_answers_and_approves(team)
    _as(who, MAYA)
    assert authority.store is not None
    before = [(o.id, o.outcome) for o in authority.store.list_outcomes(review_id, reviewer_id=MAYA)]
    _mark(client, review_id, unit, "reviewed")  # signing as "agrees" = her existing lgtm: the store dedups it
    _maya_signs_and_fails(client, review_id, revision, authority, monkeypatch, note="")
    assert [(o.id, o.outcome) for o in authority.store.list_outcomes(review_id, reviewer_id=MAYA)] == before
    latest = authority.store.latest_outcome(review_id, MAYA)
    assert latest is not None and latest.outcome == "lgtm"


def test_a_failure_to_ask_the_second_reviewer_never_fails_the_verdict(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, review_id, revision, who, authority = team

    def boom(_outcome: Any) -> None:
        raise RuntimeError("the directory is down")

    monkeypatch.setattr(client.app.state, "review_second_reviewer_on_verdict", boom)
    _as(who, DANA)
    posted = client.post(
        f"/api/reviews/{review_id}/outcome",
        headers=HEADERS,
        json={"outcome": "lgtm", "summary": "", "expected_revision_id": revision},
    )
    assert posted.status_code == 200, posted.text
    assert authority.store is not None
    latest = authority.store.latest_outcome(review_id, DANA)
    assert latest is not None and latest.outcome == "lgtm"


# --------------------------------------------------------------------------- void verdicts (round 5)


def _outcomes_as_seen_by(
    client: TestClient, review_id: str, who: dict[str, str], viewer: str, person: str
) -> list[tuple[str, str]]:
    _as(who, viewer)
    body = client.get(f"/api/reviews/{review_id}/activity", headers=HEADERS).json()["events"]
    return [(e["actor_id"], e["kind"]) for e in body if e["actor_id"] == person and e["kind"] == "outcome.recorded"]


def test_a_pre_ask_approval_that_was_retracted_stays_retracted_after_a_failed_signing(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, review_id, revision, who, authority = team
    unit = _unit(client, review_id)
    _as(who, MAYA)
    _verdict(client, review_id, revision, "lgtm")
    assert authority.store is not None
    first = authority.store.latest_outcome(review_id, MAYA)
    assert first is not None
    assert authority.sign(review_id, first.id, {})["status"] == "recorded"
    record = client.get(f"/api/reviews/{review_id}/record", headers=HEADERS).json()["records"][0]["record_id"]
    authority.signing.mode = "user_key"
    authority.signing.exporter_of = lambda: who["id"]
    assert client.post(f"/api/reviews/{review_id}/records/{record}/retract", headers=HEADERS).status_code == 201
    authority.signing.mode = "organization_key"
    authority.signing.exporter_of = None
    _as(who, SAM)
    _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "needs_changes")
    _maya_signs_and_fails(client, review_id, revision, authority, monkeypatch)
    shown = _condition(client, review_id)
    assert (shown["state"], shown["reason"]) == ("pending", "second_signature_pending")  # never "satisfied"
    assert [o.id for o in authority.store.list_outcomes(review_id, reviewer_id=MAYA)] == [first.id]  # no phantom row
    assert _outcomes_as_seen_by(client, review_id, who, SAM, MAYA) == [(MAYA, "outcome.recorded")]  # her real one only


def test_a_real_request_for_changes_from_before_the_ask_still_fails_readiness_after_a_failed_signing(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, review_id, revision, who, authority = team
    unit = _unit(client, review_id)
    _as(who, MAYA)
    _verdict(client, review_id, revision, "changes_requested")
    _as(who, SAM)
    _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    _maya_signs_and_fails(client, review_id, revision, authority, monkeypatch, note="")
    shown = _condition(client, review_id)
    assert (shown["state"], shown["reason"]) == ("fail", "changes_requested")
    assert authority.store is not None
    latest = authority.store.latest_outcome(review_id, MAYA)
    assert latest is not None and latest.outcome == "changes_requested"


def test_a_failed_signing_with_no_earlier_decision_leaves_nothing_of_hers_that_counts(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, review_id, revision, who, authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "needs_changes")
    _maya_signs_and_fails(client, review_id, revision, authority, monkeypatch)
    assert authority.store is not None
    assert authority.store.latest_outcome(review_id, MAYA) is None
    assert authority.store.list_outcomes(review_id, reviewer_id=MAYA) == ()
    assert [row.reviewer_id for row in authority.store.latest_outcomes(review_id)] == [SAM]
    shown = _condition(client, review_id)
    assert (shown["state"], shown["reason"]) == ("pending", "second_signature_pending")
    # and nobody reads it: not Sam, not the activity feed
    assert _outcomes_as_seen_by(client, review_id, who, SAM, MAYA) == []
    _as(who, MAYA)
    assert _status(client, review_id)["state"] == "waiting"
    # she can sign again and it counts
    monkeypatch.undo()
    again = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign",
        headers=HEADERS,
        json={"expected_revision_id": revision, "note": "Guard it."},
    )
    assert again.status_code == 200 and again.json()["state"] == "disagrees"


def test_a_repeat_of_a_voided_verdict_is_recorded_afresh_not_handed_back(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, review_id, revision, who, authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    _maya_signs_and_fails(client, review_id, revision, authority, monkeypatch, note="")
    monkeypatch.undo()
    assert authority.store is not None and authority.store.latest_outcome(review_id, MAYA) is None
    again = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert again.status_code == 200 and again.json()["signatures"] == {"have": 2, "needed": 2}


# --------------------------------------------------------------------------- #
# P9-4: the lens (anti-anchoring over every read) and the second reviewer's note
# --------------------------------------------------------------------------- #

LENS_NOTE = "Sam's private reasoning about the symlink"
#: Reads whose body may not change for her, with the fields a clock or a cursor moves on their own.
ROUTES = (
    "",
    "/targets",
    "/annotations",
    "/activity",
    "/readiness",
    "/frontier",
    "/finish",
    "/revisions",
    "/outcome",
    "/projection",
    "/journal",
    "/live-state",
)
VOLATILE = {"evaluated_at", "generated_at", "cursor", "annotations_cursor", "journal_cursor", "now", "updated_at"}


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _reads(client: TestClient, review_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for route in ROUTES:
        response = client.get(f"/api/reviews/{review_id}{route}", headers=HEADERS)
        assert response.status_code == 200, (route, response.text)
        out[route] = _stable(response.json())
    return out


def test_before_she_answers_the_second_reviewer_reads_the_review_as_if_the_first_had_done_nothing(team: Team) -> None:
    client, review_id, _revision, who, _authority = team
    _as(who, MAYA)
    before = _reads(client, review_id)
    _sam_answers_and_approves(team, note=LENS_NOTE)
    _as(who, MAYA)
    after = _reads(client, review_id)
    assert {route for route in ROUTES if before[route] != after[route]} == set()
    # nor does any of it appear as text: his note, his verdict's id, a count of comments, a started flag
    assert LENS_NOTE not in json.dumps(after)


def test_the_lens_is_lifted_when_she_has_answered_and_never_for_the_first_reviewer(team: Team) -> None:
    client, review_id, _revision, who, _authority = team
    unit = _sam_answers_and_approves(team, note=LENS_NOTE)
    _as(who, SAM)
    own = _reads(client, review_id)
    assert LENS_NOTE in json.dumps(own) and own["/finish"]["open_comments"] == 1
    _as(who, MAYA)
    assert _reads(client, review_id)["/finish"]["open_comments"] == 0
    _mark(client, review_id, unit, "reviewed")
    shown = _reads(client, review_id)
    assert LENS_NOTE in json.dumps(shown) and shown["/finish"]["open_comments"] == 1


def test_her_status_carries_no_verdict_and_no_signature_count_until_she_answers(team: Team) -> None:
    client, review_id, _revision, who, _authority = team
    unit = _sam_answers_and_approves(team, note=LENS_NOTE)
    _as(who, MAYA)
    hidden = client.get(f"/api/reviews/{review_id}/second-reviewer", headers=HEADERS).json()
    assert (hidden["role"], hidden["state"], hidden["first_answers_hidden"]) == ("second", "waiting", True)
    assert hidden["first"] is None and hidden["signatures"] is None
    assert hidden["first_reviewer"]["name"] == "Sam Rivera"  # SPEC: "Sam Rivera reviewed this change"
    _mark(client, review_id, unit, "reviewed")
    shown = client.get(f"/api/reviews/{review_id}/second-reviewer", headers=HEADERS).json()
    assert shown["first"]["outcome"] == "lgtm" and shown["signatures"] == {"have": 1, "needed": 2}
    # the first reviewer is told the count all along
    _as(who, SAM)
    own = client.get(f"/api/reviews/{review_id}/second-reviewer", headers=HEADERS).json()
    assert own["signatures"] == {"have": 1, "needed": 2}


def test_a_second_signature_cannot_be_retracted_on_a_hosted_review_and_its_ask_stays_signed(team: Team) -> None:
    """SPEC 7.1 P9-4 E: retraction is local (P0-2), and a hosted organization key is refused (P6-2 C), so a second
    signature is permanent there and its ask never needs to go back to waiting."""

    client, review_id, revision, who, authority = team
    unit = _sam_answers_and_approves(team)
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    signed = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert signed.status_code == 200, signed.text
    record = signed.json()["second"]["record_id"]
    assert authority.signing.mode == "organization_key"  # a hosted review: the organization signs, not the person
    authority.signing.exporter_of = lambda: MAYA  # even as the person who signed
    refused = client.post(f"/api/reviews/{review_id}/records/{record}/retract", headers=HEADERS)
    assert refused.status_code == 409 and "not_signed_here" in refused.text, refused.text
    assert _sams_ask(authority, review_id) == "signed"
    after = _status(client, review_id)
    assert (after["state"], after["signatures"]) == ("agrees", {"have": 2, "needed": 2})


def test_the_scope_belongs_to_the_task_that_set_it_and_a_block_gives_it_back() -> None:
    """Two requests interleaved on one event loop, one thread: what one hides the other still reads."""

    import asyncio

    seen: dict[str, frozenset[str]] = {}

    async def hidden_request() -> None:
        viewer_lens.enter("rev", {SAM})
        await asyncio.sleep(0)  # the other request runs now
        seen["hidden_after_yield"] = viewer_lens.hidden_in("rev")

    async def other_request() -> None:
        await asyncio.sleep(0)
        seen["other"] = viewer_lens.hidden_in("rev")

    async def blocks() -> None:
        with viewer_lens.scoped("rev", {SAM}):
            seen["inside"] = viewer_lens.hidden_in("rev")
            with viewer_lens.unfiltered():
                seen["lifted"] = viewer_lens.hidden_in("rev")
            seen["back"] = viewer_lens.hidden_in("rev")
        seen["after"] = viewer_lens.hidden_in("rev")

    async def main() -> None:
        await asyncio.gather(hidden_request(), other_request())
        await blocks()

    asyncio.run(main())
    assert seen["hidden_after_yield"] == {SAM} and seen["other"] == frozenset()
    assert [seen[k] == {SAM} for k in ("inside", "lifted", "back")] == [True, False, True]
    assert seen["after"] == frozenset()


def test_what_a_write_returns_is_built_as_she_may_read_it(team: Team) -> None:
    """Her own mark's response (progress, outline, priorities) carries nothing of his, not even his note's reason code."""

    client, review_id, revision, who, _authority = team
    _sam_answers_and_approves(team, note=LENS_NOTE)
    _as(who, MAYA)
    unit = _unit(client, review_id)
    posted = client.post(
        f"/api/reviews/{review_id}/marks",
        headers=HEADERS,
        json={"unit_key": unit, "state": "unreviewed", "expected_revision_id": revision},  # not an answer yet
    )
    assert posted.status_code == 200, posted.text
    assert "request_change_open" not in posted.text and LENS_NOTE not in posted.text
    assert client.get(f"/api/reviews/{review_id}/second-reviewer", headers=HEADERS).json()["answered"] is False
    assert posted.json()["priorities"]  # a queue was returned, and it is the one she may read


def _force_a_policy_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make readiness say the policy changed ("oldgen" -> "newgen"), pending on whatever else is unsatisfied."""

    from lemoncrow.pro.capabilities.review import readiness as R

    def forced(inputs: Any, conditions: Any) -> tuple[Any, ...]:
        blockers = [
            c for c in conditions if c.owner != "scm" and not c.satisfied and c.reason not in R.TRANSIENT_REASONS
        ]
        return (
            R.ReadinessCondition(
                id="policy-generation",
                label="Review policy changed",
                state="pending" if blockers else "pass",
                detail="changed",
                category="policy",
                reason="policy_changed" if blockers else "revalidated",
                extra={"previous_generations": ["oldgen"], "generation": "newgen"},
            ),
        )

    monkeypatch.setattr(R, "_policy_change", forced)


def test_a_viewer_with_nothing_hidden_keeps_persisting_and_writing_plain_events(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full-rows read is only for a lensed viewer: Sam's own readiness read stores his record and writes the whole,
    unmarked event."""

    _force_a_policy_change(monkeypatch)
    client, review_id, revision, who, authority = team
    _sam_answers_and_approves(team, note=LENS_NOTE)
    _as(who, SAM)
    body = client.get(f"/api/reviews/{review_id}/readiness", headers=HEADERS).json()
    assert authority.store is not None
    [event] = authority.store.list_activity_events(review_id, kinds=("policy.generation_changed",))
    assert "from_full_rows" not in event.detail_json and "blocked_by" in event.detail_json
    from lemoncrow.pro.capabilities.review.workflow_state import READINESS_SUMMARY_KIND, readiness_scope

    stored = authority.store.derived_results(READINESS_SUMMARY_KIND, [revision]).get((revision, readiness_scope(SAM)))
    assert stored is not None and stored[1]["blocker_count"] == body["blocker_count"]


def test_a_read_of_readiness_by_a_hidden_viewer_notes_the_policy_change_from_the_full_rows(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GET of readiness decides (an armed landing) and writes a shared event (`policy.generation_changed`). Both run
    on the readiness of the full rows, whoever asked; only the answer she is given is narrowed."""

    _force_a_policy_change(monkeypatch)
    client, review_id, _revision, who, authority = team
    _sam_answers_and_approves(team, note=LENS_NOTE)  # his must-fix note is an unresolved human comment
    _as(who, MAYA)
    body = client.get(f"/api/reviews/{review_id}/readiness", headers=HEADERS).json()
    comments = next(c for c in body["conditions"] if c["id"] == "human-change-requests")
    assert comments["state"] == "pass"  # what she is told: as if he had written nothing
    assert authority.store is not None
    [event] = authority.store.list_activity_events(review_id, kinds=("policy.generation_changed",))
    noted = json.loads(event.detail_json)
    assert "Unresolved request-change comments" in noted["blocked_by"]  # what the shared event records: the full rows
    assert LENS_NOTE not in event.detail_json
    # ...but the event reaches her feed without the words that came from the rows she may not read yet (she still learns
    # that the policy changed), while Sam, who may read them, sees the whole event
    hers = [
        e
        for e in client.get(f"/api/reviews/{review_id}/activity", headers=HEADERS).json()["events"]
        if e["kind"] == "policy.generation_changed"
    ]
    [mine] = hers
    assert mine["summary"] == "Review policy changed (generation oldgen -> newgen)" or mine["summary"].startswith(
        "Review policy changed (generation "
    )
    assert "not satisfied" not in mine["summary"] and "satisfy" not in mine["summary"]
    assert set(mine["detail"]) <= {"origin", "generation", "previous_generations", "from_full_rows"}
    assert SAM not in json.dumps(mine) and "Unresolved" not in json.dumps(mine)
    _as(who, SAM)
    [full_event] = [
        e
        for e in client.get(f"/api/reviews/{review_id}/activity", headers=HEADERS).json()["events"]
        if e["kind"] == "policy.generation_changed"
    ]
    assert "Unresolved request-change comments" in full_event["detail"]["blocked_by"]
    _as(who, MAYA)
    # ...and the record her Inbox row reads is the answer she was given, not the one the decision was made on
    from lemoncrow.pro.capabilities.review.workflow_state import READINESS_SUMMARY_KIND, readiness_scope

    stored = authority.store.derived_results(READINESS_SUMMARY_KIND, [_revision]).get(
        (_revision, readiness_scope(MAYA))
    )
    assert stored is not None and stored[1]["blocker_count"] == body["blocker_count"]


def test_an_armed_landing_is_settled_by_a_hidden_viewers_read_from_the_full_rows_and_says_so_only_in_words_she_may_read(
    team: Team,
) -> None:
    """The armed-landing branch of a readiness read: the decision is made on the full rows, what it writes about it is
    redacted for the viewer who triggered it, and the record her Inbox row reads stays the answer she was given."""

    from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship

    client, review_id, revision, who, authority = team
    _sam_answers_and_approves(team, note=LENS_NOTE)
    assert authority.store is not None
    authority.store.upsert_relationship(
        ReviewRelationship(
            id="",
            review_id=review_id,
            kind="landing",
            relation="merge_when_ready",
            provider="github",
            external_id="acme/widgets#7",
            title="Merge when ready",
            status="armed",
            source="human",
            metadata_json=json.dumps(
                {"revision_id": revision, "conditions_hash": "old", "policy_generation": "oldgen", "armed_by": SAM}
            ),
        )
    )
    _as(who, MAYA)
    shown = client.get(f"/api/reviews/{review_id}/readiness", headers=HEADERS)
    assert shown.status_code == 200, shown.text
    assert LENS_NOTE not in shown.text
    landing_events = [
        e
        for e in client.get(f"/api/reviews/{review_id}/activity", headers=HEADERS).json()["events"]
        if e["kind"].startswith("landing.")
    ]
    assert landing_events, "the read settled the armed landing and said so"
    for event in landing_events:
        assert "Unresolved" not in json.dumps(event) and "request-change" not in json.dumps(event)
        assert set(event["detail"]) <= {
            "origin",
            "generation",
            "previous_generations",
            "action",
            "kind",
            "state",
            "from_full_rows",
        }
    from lemoncrow.pro.capabilities.review.workflow_state import READINESS_SUMMARY_KIND, readiness_scope

    stored = authority.store.derived_results(READINESS_SUMMARY_KIND, [revision]).get((revision, readiness_scope(MAYA)))
    assert stored is not None and stored[1]["blocker_count"] == shown.json()["blocker_count"]
    # R5-2: the withdrawn landing's words (the blockers, who armed it) are not hers: the readiness answer's landing block
    # and /relationships give its state and nothing of what it was made from
    assert "Unresolved" not in json.dumps(shown.json()["landing"]) and "request-change" not in json.dumps(
        shown.json()["landing"]
    )
    [row] = client.get(f"/api/reviews/{review_id}/relationships", headers=HEADERS).json()["landing"]
    assert row["status"] and set(row["metadata"]) <= {
        "provider",
        "provider_cancel",
        "cancel_reason",
        "cancel_state",
        "provider_state",
        "state",
    }
    assert SAM not in json.dumps(row) and "Unresolved" not in json.dumps(row)
    _as(who, SAM)
    [whole] = client.get(f"/api/reviews/{review_id}/relationships", headers=HEADERS).json()["landing"]
    assert whole["metadata"].get("armed_by") == SAM and "Unresolved request-change comments" in json.dumps(
        whole["metadata"]
    )
    assert "Unresolved request-change comments" in json.dumps(
        client.get(f"/api/reviews/{review_id}/readiness", headers=HEADERS).json()["landing"]
    )


def test_an_event_someone_elses_read_wrote_reaches_a_hidden_viewer_redacted(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5-1: the first reader writes the policy event for everyone (once per generation pair). If that reader is Sam, the
    event carries no mark, and Maya still reads it through the allowlist: she learns the policy changed, nothing more.
    """

    _force_a_policy_change(monkeypatch)
    client, review_id, _revision, who, authority = team
    _sam_answers_and_approves(team, note=LENS_NOTE)
    _as(who, SAM)
    client.get(f"/api/reviews/{review_id}/readiness", headers=HEADERS)  # Sam reads first: nothing is hidden from him
    assert authority.store is not None
    [stored] = authority.store.list_activity_events(review_id, kinds=("policy.generation_changed",))
    assert "from_full_rows" not in stored.detail_json and "blocked_by" in stored.detail_json  # unmarked, and whole
    _as(who, MAYA)
    [hers] = [
        e
        for e in client.get(f"/api/reviews/{review_id}/activity", headers=HEADERS).json()["events"]
        if e["kind"] == "policy.generation_changed"
    ]
    assert "not satisfied" not in hers["summary"] and set(hers["detail"]) <= {
        "origin",
        "generation",
        "previous_generations",
    }
    assert "blocked_by" not in json.dumps(hers) and "Unresolved" not in json.dumps(hers)
    _as(who, SAM)
    [his] = [
        e
        for e in client.get(f"/api/reviews/{review_id}/activity", headers=HEADERS).json()["events"]
        if e["kind"] == "policy.generation_changed"
    ]
    assert "Unresolved request-change comments" in his["detail"]["blocked_by"]  # Sam still reads the whole event


@pytest.mark.parametrize(
    ("state", "line"),
    [
        ("armed", "Merge when ready is on."),
        ("delegated", "Merge when ready is on."),
        ("queued", "Merge when ready is on."),
        ("merged", ""),
        ("blocked", "Merge when ready is not in force."),
        ("cancelled", "Merge when ready is not in force."),
        ("failed", "Merge when ready is not in force."),
        ("not_armed", "Merge when ready is not in force."),
    ],
)
def test_the_line_a_hidden_reader_gets_for_a_landing_says_its_state_and_nothing_else(state: str, line: str) -> None:
    wire = {
        "state": state,
        "armed": state == "armed",
        "detail": "withdrawn: Unresolved request-change comments",
        "bypassed": ["x"],
    }
    with viewer_lens.scoped("r", {SAM}):
        hers = viewer_lens.landing_wire("r", wire)
    assert hers["detail"] == line and hers["bypassed"] == [] and hers["state"] == state
    assert viewer_lens.landing_wire("r", wire)["detail"] == wire["detail"]  # nothing hidden: the whole line


def test_a_hidden_viewer_may_not_finish_the_review(team: Team) -> None:
    client, review_id, _revision, who, _authority = team
    _sam_answers_and_approves(team, note=LENS_NOTE)
    _as(who, MAYA)
    refused = client.post(f"/api/reviews/{review_id}/finish", headers=HEADERS, json={"status": "finished"})
    assert refused.status_code == 409 and LENS_NOTE not in refused.text


def test_her_disagreement_is_a_must_fix_note_in_the_set_the_author_reads_with_the_first_reviewers(team: Team) -> None:
    client, review_id, revision, who, _authority = team
    unit = _sam_answers_and_approves(team, note=LENS_NOTE)
    _as(who, MAYA)
    _mark(client, review_id, unit, "needs_changes")
    signed = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign",
        headers=HEADERS,
        json={"expected_revision_id": revision, "note": "Do not follow symlinks here."},
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["second"]["note_sent"] == {"count": 1, "to": "the author"}
    _as(who, SAM)
    notes = client.get(f"/api/reviews/{review_id}/annotations", headers=HEADERS).json()["annotations"]
    theirs = [note for note in notes if note["created_by"] == MAYA]
    assert [(note["body"], note["kind"], note["state"]) for note in theirs] == [
        ("Do not follow symlinks here.", "request_change", "open")
    ]
    assert LENS_NOTE in {note["body"] for note in notes}  # both notes, in one set
    # one note, however often the status is read
    for _ in range(2):
        again = client.get(f"/api/reviews/{review_id}/second-reviewer", headers=HEADERS).json()
        assert again["second"]["note_sent"] == {"count": 1, "to": "the author"}
    blocked = client.get(f"/api/reviews/{review_id}/readiness", headers=HEADERS).json()
    assert blocked["conditions"] and any(c["state"] != "pass" for c in blocked["conditions"])


def test_an_agreement_publishes_no_note(team: Team) -> None:
    client, review_id, revision, who, _authority = team
    unit = _sam_answers_and_approves(team, note=LENS_NOTE)
    _as(who, MAYA)
    _mark(client, review_id, unit, "reviewed")
    signed = client.post(
        f"/api/reviews/{review_id}/second-reviewer/sign", headers=HEADERS, json={"expected_revision_id": revision}
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["second"]["note_sent"] is None
    notes = client.get(f"/api/reviews/{review_id}/annotations", headers=HEADERS).json()["annotations"]
    assert [note for note in notes if note["created_by"] == MAYA] == []
    assert _unit(client, review_id)  # the review is still readable


# --------------------------------------------------------------------------- review round 7


def _seed_ask_turns(team: Team) -> tuple[str, str]:
    """Sam's conversation (a question and the AI's answer), one row from before authors were recorded, and Maya's own."""

    from lemoncrow.pro.capabilities.review.session_models import ReviewAskMessage

    _client, review_id, revision, _who, authority = team
    store = authority.store

    def turns(conversation_id: str, author: str, ask: str, answer: str) -> None:
        for role, body in (("human", ask), ("assistant", answer)):
            store.append_ask_message(
                ReviewAskMessage(
                    id="",
                    review_id=review_id,
                    revision_id=revision,
                    role=role,
                    intent="question",
                    body=body,
                    conversation_id=conversation_id,
                    created_by=author,
                )
            )

    mayas = store.create_ask_conversation(review_id, created_by=MAYA).id
    turns(mayas, MAYA, "Maya asks about charge", "The AI answers Maya about charge")
    sams = store.create_ask_conversation(review_id, created_by=SAM).id  # the newest
    turns(sams, SAM, "Sam asks about the symlink", "The AI answers Sam about the symlink")
    turns("", "", "A question from before authors were recorded", "An old answer")
    return sams, mayas


def _ask_text(client: TestClient, review_id: str, **params: str) -> str:
    return client.get(f"/api/reviews/{review_id}/ask", headers=HEADERS, params=params).text


def test_the_first_reviewers_ask_history_is_withheld_from_the_second_until_she_answers(team: Team) -> None:
    client, review_id, _revision, who, authority = team
    sams, mayas = _seed_ask_turns(team)
    _sam_answers_and_approves(team)
    _as(who, SAM)
    sam_sees = _ask_text(client, review_id)
    assert "Sam asks about the symlink" in sam_sees
    _as(who, MAYA)
    maya_sees = _ask_text(client, review_id)
    assert "Sam asks about the symlink" not in maya_sees and "The AI answers Sam" not in maya_sees
    assert "A question from before authors" not in maya_sees  # unauthored rows fail closed
    assert "Maya asks about charge" in maya_sees  # her own history is hers
    # She cannot open his conversation by its id, nor continue it, nor have a limit spent on rows she may not read.
    assert (
        client.get(f"/api/reviews/{review_id}/ask", headers=HEADERS, params={"conversation_id": sams}).status_code
        == 404
    )
    assert (
        client.get(f"/api/reviews/{review_id}/ask", headers=HEADERS, params={"conversation_id": mayas}).status_code
        == 200
    )
    assert authority.store.list_ask_conversations(review_id, limit=1)[0].id == sams  # unlensed: his is the newest
    # After she answers there is nothing left to hide.
    _mark(client, review_id, _unit(client, review_id), "reviewed")
    assert "Sam asks about the symlink" in _ask_text(client, review_id)


def test_a_limit_counts_only_the_ask_rows_the_reader_may_see(team: Team) -> None:
    from lemoncrow.pro.capabilities.review import viewer_lens

    _client, review_id, _revision, _who, authority = team
    sams, mayas = _seed_ask_turns(team)  # Sam's conversation is the newest
    store = authority.store
    assert store.list_ask_conversations(review_id, limit=1)[0].id == sams
    with viewer_lens.scoped(review_id, {SAM}):
        assert [c.id for c in store.list_ask_conversations(review_id, limit=1)] == [mayas]
        assert store.get_ask_conversation(sams) is None
        assert store.get_ask_conversation(mayas) is not None
        assert {m.created_by for m in store.list_ask_messages(review_id)} == {MAYA}
        assert len(store.list_ask_messages(review_id, limit=1)) == 1


def test_the_second_reviewer_cannot_hand_off_the_first_reviewers_ask_answer(team: Team) -> None:
    client, review_id, _revision, who, authority = team
    sams, _mayas = _seed_ask_turns(team)
    _sam_answers_and_approves(team)
    answer = next(
        m for m in authority.store.list_ask_messages(review_id, conversation_id=sams) if m.role == "assistant"
    )
    _as(who, MAYA)
    handoff = client.post(f"/api/reviews/{review_id}/ask/{answer.id}/handoff", headers=HEADERS, json={})
    assert handoff.status_code == 404 and "no such Ask message" in handoff.text, handoff.text


# --------------------------------------------------------------------------- review round 9


def test_a_reply_to_the_first_reviewers_note_is_refused_as_for_a_note_that_does_not_exist(team: Team) -> None:
    client, review_id, revision, who, authority = team
    unit = _sam_answers_and_approves(team, note="Sam's private reasoning")
    _as(who, SAM)
    note = client.get(f"/api/reviews/{review_id}/annotations", headers=HEADERS).json()["annotations"][0]["id"]
    before = authority.store.get_annotation(note)

    def reply(parent: str) -> Any:
        return client.post(
            f"/api/reviews/{review_id}/annotations",
            headers=HEADERS,
            json={
                "path": "link.txt",
                "start_line": 0,
                "end_line": 0,
                "file_level": True,
                "body": "a reply",
                "kind": "comment",
                "parent_id": parent,
                "target_unit_key": unit,
                "expected_revision_id": revision,
            },
        )

    _as(who, MAYA)
    his, never = reply(note), reply(note[:-6] + "000000")
    assert his.status_code == never.status_code and his.status_code in {400, 404, 409, 422}, (his.text, never.text)
    assert his.text.replace(note, "ID") == never.text.replace(note[:-6] + "000000", "ID")
    assert authority.store.get_annotation(note) == before  # his note was not re-versioned or reopened
    _mark(client, review_id, unit, "reviewed")  # once she has answered, the note is hers to reply to
    assert reply(note).status_code in {200, 201}


def test_a_priority_override_the_first_reviewer_set_is_not_read_by_the_second_in_the_inbox_bulk_read(
    tmp_path: Path,
) -> None:
    """The Inbox reads override events for many Reviews at once; his reason and his id are his, until she has answered."""

    from lemoncrow.pro.capabilities.review.review_triage import (
        PRIORITY_OVERRIDE_KINDS,
        active_override,
        priority_override_event,
    )
    from lemoncrow.pro.capabilities.review.store import new_session_id

    store = ReviewStore(tmp_path / "store")
    session = store.create_session(
        ReviewSession(
            id=new_session_id(), subject_type="pull_request", repo_root="/repo", range_mode="commit_range", title="t"
        )
    )
    store.record_activity(
        priority_override_event(
            review_id=session.id,
            revision_id="",
            level="urgent",
            reason="the token check is bypassed",
            scope="review",
            actor_id="sam",
            actor_type="human",
        )
    )

    def read() -> dict[str, Any]:
        return store.activity_events_for_reviews([session.id], kinds=PRIORITY_OVERRIDE_KINDS)

    assert [e.actor_id for e in read()[session.id]] == ["sam"]
    with viewer_lens.scoped(session.id, frozenset({"sam"})):
        assert read() == {}
        assert active_override(read().get(session.id, ()), current_revision_id="") is None
    with viewer_lens.scoped(session.id, frozenset({"someone-else"})):
        assert [e.actor_id for e in read()[session.id]] == ["sam"]
