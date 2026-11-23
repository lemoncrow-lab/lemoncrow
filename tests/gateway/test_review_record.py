"""Phase 10 item 19: signed review records (DSSE + in-toto v1), local signing and the verifier.

Pins what may never happen: a record whose payload digest cannot be
reproduced from the store, a tampered record verifying, a record signed after
its key's revocation verifying, a rotated-out key orphaning the records it
signed, an unsigned record presented as signed (in the payload, the API or
the CLI), a personal key living inside a repository, and an export with no
decision.

Keys are generated at test runtime; no key material is committed.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.core.settings import write_setting
from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.record_signing import (
    USER_KEY_SETTING,
    Ed25519RecordSigner,
    RecordSigningUnavailable,
    dsse_envelope,
    generate_user_key,
    key_set_document,
    load_user_key,
    public_key_entry,
    refuse_key_location,
    user_key_id,
)
from lemoncrow.pro.capabilities.review.record_verify import EXIT_CODES, parse_key_set, verify_record
from lemoncrow.pro.capabilities.review.review_record import (
    PAYLOAD_TYPE,
    PREDICATE_TYPE,
    PREDICATE_TYPE_V2,
    STATEMENT_TYPE,
    UNSIGNED_NOTICE,
    SigningIdentity,
    canonical_json,
    compare_with_store,
    sha256_digest,
    validate_statement,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from tests.gateway.test_review_api import (
    _BASE_SOURCE,
    _CHANGED_SOURCE,
    BASE,
    PORT,
    TOKEN,
    Workspace,
    _commit,
    _init_repo,
    _no_astgrep_download,
    _write,
)

__all__ = ["_no_astgrep_download"]


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@dataclass
class RecordWorkspace:
    workspace: Workspace
    app: FastAPI
    settings_root: Path

    @property
    def review_id(self) -> str:
        return self.workspace.review_id

    def latest_id(self) -> str:
        revision = self.workspace.store.latest_revision(self.review_id)
        assert revision is not None
        return revision.id

    def approve(self, outcome: str = "lgtm") -> None:
        recorded = self.workspace.post(
            f"/api/reviews/{self.review_id}/outcome", json={"outcome": outcome, "summary": "Checked."}
        )
        assert recorded.status_code == 200, recorded.text

    def status(self) -> dict[str, Any]:
        response = self.workspace.get(f"/api/reviews/{self.review_id}/record")
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    def export(self, signing: str = "auto", **body: Any) -> Any:
        return self.workspace.post(
            f"/api/reviews/{self.review_id}/record",
            json={"signing": signing, "expected_revision_id": self.latest_id(), **body},
        )

    def opt_in(self) -> tuple[Path, str]:
        """``lc review record-key init`` equivalent: a runtime-generated owner-only key."""

        key_path = self.settings_root / "review-signing" / "user-key.pem"
        key_id = generate_user_key(key_path)
        write_setting(self.settings_root, USER_KEY_SETTING, str(key_path))
        return key_path, key_id


@pytest.fixture
def record_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RecordWorkspace:
    settings_root = tmp_path / "lemoncrow-root"
    settings_root.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(settings_root))
    repo_root = tmp_path / "repo"
    repo = _init_repo(repo_root)
    _write(repo_root, "src/session.py", _BASE_SOURCE)
    _commit(repo, "base", 0)
    _write(repo_root, "src/session.py", _CHANGED_SOURCE)
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, None, working_tree=True)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    revision = store.latest_revision(session.id)
    assert revision is not None
    store.publish_snapshot(session.id, revision.id)
    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency(TOKEN), repo_root=repo_root, port=PORT)
    workspace = Workspace(TestClient(app, base_url=BASE), store, store_root, repo_root, session.id)
    return RecordWorkspace(workspace, app, settings_root)


def _payload(envelope: Mapping[str, Any]) -> dict[str, Any]:
    statement: dict[str, Any] = json.loads(base64.b64decode(envelope["payload"]))
    return statement


def _resign(statement: Mapping[str, Any], signer: Any) -> dict[str, Any]:
    return dsse_envelope(canonical_json(statement), signer)


def _with_issued_at(statement: Mapping[str, Any], instant: datetime) -> dict[str, Any]:
    copy = json.loads(json.dumps(statement))
    copy["predicate"]["issued_at"] = copy["predicate"]["as_of"] = instant.isoformat()
    return dict(copy)


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _signer_for(statement: Mapping[str, Any], key: Ed25519PrivateKey) -> Ed25519RecordSigner:
    signing = statement["predicate"]["signing"]
    return Ed25519RecordSigner(key, key_id=signing["key_id"], mode=signing["mode"])


def _keys(*entries: dict[str, Any]) -> dict[str, Any]:
    return parse_key_set(key_set_document(list(entries), issuer=str(entries[0]["issuer"]) if entries else ""))


class _Es256Signer:
    """An ES256 signer (the KMS-shaped algorithm) to prove the verifier is not Ed25519-only."""

    def __init__(self, key: ec.EllipticCurvePrivateKey, key_id: str) -> None:
        self._key = key
        self.identity = SigningIdentity(mode="organization_key", key_id=key_id, algorithm="ES256")

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message, ec.ECDSA(hashes.SHA256()))


# --------------------------------------------------------------------------- #
# export: verifies, matches the store, reproduces the payload digest
# --------------------------------------------------------------------------- #


def test_signed_export_verifies_matches_the_store_and_reproduces_its_digest(
    record_workspace: RecordWorkspace,
) -> None:
    ws = record_workspace
    _key_path, key_id = ws.opt_in()
    ws.approve()
    status = ws.status()
    assert status["decision"]["outcome"] == "approved" and status["decision"]["kind"] == "human_outcome"
    assert status["signing"]["available"] is True and status["signing"]["key_id"] == key_id

    exported = ws.export()
    assert exported.status_code == 201, exported.text
    body = exported.json()
    record = body["record"]
    assert record["signing_state"] == "signed" and record["key_id"] == key_id
    assert record["label"] == f"Signed with personal key {key_id}"
    assert body["verification"]["status"] == "verified", body["verification"]
    assert body["matches_store"] is True, body["store"]
    assert set(body["store"].values()) == {"match"}, body["store"]
    # The payload digest is recomputed from stored rows, not echoed.
    assert body["reproduced"] is True and body["reproduced_digest"] == record["payload_digest"]

    envelope = body["envelope"]
    assert envelope["payloadType"] == PAYLOAD_TYPE and len(envelope["signatures"]) == 1
    payload = base64.b64decode(envelope["payload"])
    assert sha256_digest(payload) == record["payload_digest"]
    statement = _payload(envelope)
    assert payload == canonical_json(statement)
    assert statement["_type"] == STATEMENT_TYPE and statement["predicateType"] == PREDICATE_TYPE_V2
    assert validate_statement(statement) == []
    predicate = statement["predicate"]
    revision = ws.workspace.store.get_revision(ws.latest_id())
    assert revision is not None
    assert statement["subject"][0]["digest"]["lemoncrowTreeFingerprint"] == revision.tree_fingerprint
    assert predicate["snapshot"]["revision_id"] == revision.id and predicate["snapshot"]["number"] == 0
    assert predicate["decision"]["actor"]["id"] == "local" and predicate["decision"]["outcome"] == "approved"
    assert predicate["policy_generation"]["digest"].startswith("policy:")
    assert predicate["readiness"]["evaluated_at"] == predicate["issued_at"]
    assert predicate["readiness"]["conditions"], "readiness conditions are bound"
    assert predicate["run_plan"]["state"] in {"planned", "no_plan"}
    assert predicate["judgments"]["outcomes"][0]["outcome"] == "lgtm"
    assert predicate["review"]["repository"] and str(ws.workspace.repo_root) not in json.dumps(statement)

    # Stored and re-served byte-for-byte; the downloaded file verifies offline.
    record_id = record["record_id"]
    served = ws.workspace.get(f"/api/reviews/{ws.review_id}/records/{record_id}")
    assert served.status_code == 200 and served.headers["X-LemonCrow-Record-Signing"] == "signed"
    assert "attachment" in served.headers["Content-Disposition"]
    keys = parse_key_set(_local_keys(ws))
    assert verify_record(served.json(), keys=keys).status == "verified"
    listed = ws.status()["records"]
    # (Recording the verdict also signed it in the background, so the list may hold that record too.)
    assert record_id in [row["record_id"] for row in listed]
    activity = ws.workspace.store.list_activity_events(ws.review_id, kinds=("record.exported",))
    exported_digests = [json.loads(event.detail_json)["payload_digest"] for event in activity]
    assert record["payload_digest"] in exported_digests

    # Rows appended after the record was issued never change what it binds.
    ws.workspace.store.add_evidence(
        ReviewEvidence(
            id="",
            review_id=ws.review_id,
            revision_id=ws.latest_id(),
            kind="document",
            title="later note",
            source="human",
        )
    )
    again = ws.workspace.get(f"/api/reviews/{ws.review_id}/records/{record_id}/verify").json()
    assert again["store"]["evidence"] == "match" and again["verification"]["status"] == "verified"


def _local_keys(ws: RecordWorkspace) -> dict[str, Any]:
    key_path = Path(json.loads((ws.settings_root / "plugin_settings.json").read_text())[USER_KEY_SETTING])
    key = load_user_key(key_path)
    kid = user_key_id(key.public_key())
    return key_set_document([public_key_entry(key.public_key(), key_id=kid, issuer=kid)], issuer=kid)


def test_signed_record_binds_structured_after_structure_notes_without_rewriting_v1_history(
    record_workspace: RecordWorkspace,
) -> None:
    ws = record_workspace
    review_id = ws.review_id
    units = ws.workspace.store.list_units(ws.latest_id())
    target = next(item for item in units if item.kind == "symbol" and "refresh" in item.symbol)
    for state in ("reviewed", "unreviewed"):
        response = ws.workspace.post(
            f"/api/reviews/{review_id}/marks", json={"unit_key": target.unit_key, "state": state}
        )
        assert response.status_code == 200, response.text
    comment = ws.workspace.post(
        f"/api/reviews/{review_id}/annotations",
        json={
            "path": target.path,
            "start_line": 10,
            "end_line": 10,
            "side": "additions",
            "kind": "request_change",
            "body": "Retain the invalid-session guard",
            "target_unit_key": target.unit_key,
            "mark_target": True,
            "reconsidered_after_structure": True,
        },
    )
    assert comment.status_code == 201, comment.text
    note_id = comment.json()["annotation"]["id"]
    ws.opt_in()
    ws.approve(outcome="changes_requested")
    signed = ws.export()
    assert signed.status_code == 201, signed.text
    assert signed.json()["verification"]["status"] == "verified"
    assert signed.json()["matches_store"] is True
    statement = _payload(signed.json()["envelope"])
    meta = statement["predicate"]["judgments"]["structure_reconsiderations"]
    assert meta["count"] == 1 and meta["digest"].startswith("sha256:")
    assert validate_statement(statement) == []
    assert note_id not in json.dumps(statement), "review records expose only a digest, not human-note contents"
    # The attested history is frozen at export time. A later note edit must not
    # rewrite the payload that was already signed, nor stop store reproduction.
    edited = ws.workspace.patch(f"/api/annotations/{note_id}", json={"body": "Changed after signing"})
    assert edited.status_code == 200, edited.text
    from lemoncrow.pro.capabilities.review.review_record import gather_record_facts, judgments_section

    store = ws.workspace.store
    original = judgments_section(
        gather_record_facts(
            store,
            store.get_session(review_id),
            store.get_revision(ws.latest_id()),
            as_of=statement["predicate"]["as_of"],
        )
    )
    assert original == statement["predicate"]["judgments"]
    tampered = json.loads(json.dumps(statement))
    tampered["predicate"]["judgments"]["structure_reconsiderations"]["digest"] = "sha256:" + "0" * 64
    assert tampered["predicate"]["judgments"] != original


def test_a_tampered_statement_mismatches_the_store_section_by_section(record_workspace: RecordWorkspace) -> None:
    ws = record_workspace
    ws.approve()
    body = ws.export("unsigned").json()
    statement = _payload(body["envelope"])
    store = ws.workspace.store
    session = store.get_session(ws.review_id)
    revision = store.get_revision(ws.latest_id())
    assert session is not None and revision is not None
    predicate = statement["predicate"]

    def compare(changed: Mapping[str, Any]) -> dict[str, str]:
        return compare_with_store(
            changed,
            store=store,
            session=session,
            revision=revision,
            snapshot_number=0,
            current_policy_generation=predicate["policy_generation"],
            readiness_at=None,
            run_plan=None if predicate["run_plan"]["state"] == "no_plan" else predicate["run_plan"],
        )

    baseline = compare(statement)
    assert baseline["judgments"] == baseline["decision"] == baseline["snapshot"] == "match", baseline
    assert baseline["readiness"] == "not_checked"

    forged = json.loads(json.dumps(statement))
    forged["predicate"]["judgments"]["outcomes"][0]["outcome"] = "changes_requested"
    assert compare(forged)["judgments"] == "mismatch"
    forged = json.loads(json.dumps(statement))
    forged["predicate"]["decision"]["actor"]["id"] = "mallory"
    assert compare(forged)["decision"] == "mismatch"
    forged = json.loads(json.dumps(statement))
    forged["predicate"]["snapshot"]["tree_fingerprint"] = "0" * 64
    assert compare(forged)["snapshot"] == "mismatch"
    forged = json.loads(json.dumps(statement))
    first = forged["predicate"]["readiness"]["conditions"][0]
    first["state"] = "pass" if first["state"] != "pass" else "fail"
    assert compare(forged)["readiness"] == "mismatch"  # its conditions hash no longer binds it
    forged = json.loads(json.dumps(statement))
    forged["predicate"]["evidence"].append({"id": "forged", "executor_class": "", "attestation_class": ""})
    assert compare(forged)["evidence"] == "mismatch"


def test_export_needs_a_decision_on_the_latest_snapshot(record_workspace: RecordWorkspace) -> None:
    ws = record_workspace
    assert ws.status()["decision"] is None
    refused = ws.export("unsigned")
    assert refused.status_code == 409 and "no decision" in refused.json()["detail"]
    # A comment is not a decision.
    commented = ws.workspace.post(f"/api/reviews/{ws.review_id}/outcome", json={"outcome": "comment", "summary": "hm"})
    assert commented.status_code == 200, commented.text
    assert ws.status()["decision"] is None
    ws.approve()
    stale = ws.workspace.post(
        f"/api/reviews/{ws.review_id}/record", json={"signing": "unsigned", "expected_revision_id": "rev-old"}
    )
    assert stale.status_code == 409 and "newer revision" in stale.json()["detail"]
    assert ws.export("sideways").status_code == 400
    assert ws.workspace.store.list_activity_events(ws.review_id, kinds=("record.exported",)) == ()


# --------------------------------------------------------------------------- #
# unsigned: labeled in the payload, the API and the verifier
# --------------------------------------------------------------------------- #


def test_without_a_key_auto_refuses_and_unsigned_is_labeled_everywhere(record_workspace: RecordWorkspace) -> None:
    ws = record_workspace
    ws.approve()
    status = ws.status()
    assert status["signing"]["available"] is False and status["signing"]["code"] == "not_configured"
    assert "UNSIGNED" in status["signing"]["reason"]

    refused = ws.export("auto")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "not_configured"  # never silently unsigned
    assert ws.status()["records"] == []

    exported = ws.export("unsigned")
    assert exported.status_code == 201, exported.text
    body = exported.json()
    assert body["record"]["signing_state"] == "unsigned"
    assert body["record"]["label"] == "UNSIGNED record (no signature)"
    assert body["envelope"]["signatures"] == []
    signing = _payload(body["envelope"])["predicate"]["signing"]
    assert signing == {
        "state": "unsigned",
        "mode": "unsigned",
        "key_id": "",
        "algorithm": "",
        "notice": UNSIGNED_NOTICE,
    }
    assert body["verification"]["status"] == "unsigned" and body["verification"]["verified"] is False
    assert body["verification"]["headline"].startswith("UNSIGNED")
    assert body["matches_store"] is True and body["reproduced"] is True
    served = ws.workspace.get(f"/api/reviews/{ws.review_id}/records/{body['record']['record_id']}")
    assert served.headers["X-LemonCrow-Record-Signing"] == "unsigned"
    assert "unsigned.dsse.json" in served.headers["Content-Disposition"]


def test_unsigned_and_signed_states_cannot_be_swapped(record_workspace: RecordWorkspace) -> None:
    ws = record_workspace
    ws.opt_in()
    ws.approve()
    signed = ws.export().json()["envelope"]
    unsigned = ws.export("unsigned").json()["envelope"]
    keys = parse_key_set(_local_keys(ws))

    stripped = {**signed, "signatures": []}
    result = verify_record(stripped, keys=keys)
    assert result.status == "invalid" and "claims a signature" in result.reasons[0]
    dressed_up = {**unsigned, "signatures": signed["signatures"]}
    result = verify_record(dressed_up, keys=keys)
    assert result.status == "invalid" and "says unsigned" in result.reasons[0]
    assert verify_record(unsigned, keys=keys).status == "unsigned"
    assert verify_record(unsigned, keys=None).exit_code == EXIT_CODES["unsigned"] == 2


# --------------------------------------------------------------------------- #
# tamper, revocation, rotation (pure verifier)
# --------------------------------------------------------------------------- #


@pytest.fixture
def signed_record(record_workspace: RecordWorkspace) -> tuple[dict[str, Any], dict[str, Any], Ed25519PrivateKey]:
    ws = record_workspace
    key_path, _key_id = ws.opt_in()
    ws.approve()
    envelope = ws.export().json()["envelope"]
    return envelope, _local_keys(ws), load_user_key(key_path)


def test_tampering_fails_verification(signed_record: tuple[dict[str, Any], dict[str, Any], Ed25519PrivateKey]) -> None:
    envelope, key_set, key = signed_record
    keys = parse_key_set(key_set)
    assert verify_record(envelope, keys=keys).status == "verified"
    statement = _payload(envelope)

    altered = json.loads(json.dumps(statement))
    altered["predicate"]["decision"]["outcome"] = "changes_requested"
    forged = {**envelope, "payload": base64.b64encode(canonical_json(altered)).decode("ascii")}
    result = verify_record(forged, keys=keys)
    assert result.status == "invalid" and "does not match the payload" in result.reasons[0]
    assert result.exit_code == EXIT_CODES["invalid"] == 1

    flipped = bytearray(base64.b64decode(envelope["signatures"][0]["sig"]))
    flipped[0] ^= 0x01
    bad_sig = {
        **envelope,
        "signatures": [{**envelope["signatures"][0], "sig": base64.b64encode(bytes(flipped)).decode()}],
    }
    assert verify_record(bad_sig, keys=keys).status == "invalid"

    # Re-signed by someone else under the same key id.
    impostor = _resign(statement, _signer_for(statement, Ed25519PrivateKey.generate()))
    assert verify_record(impostor, keys=keys).status == "invalid"

    # Same JSON, non-canonical bytes: the digest would not be reproducible.
    pretty = json.dumps(statement, indent=2).encode("utf-8")
    loose = dsse_envelope(pretty, _signer_for(statement, key))
    result = verify_record(loose, keys=keys)
    assert result.status == "invalid" and "canonical" in result.reasons[0]

    wrong_type = {**envelope, "payloadType": "application/json"}
    assert verify_record(wrong_type, keys=keys).status == "invalid"
    renamed = {**envelope, "signatures": [{**envelope["signatures"][0], "keyid": "user-other"}]}
    assert verify_record(renamed, keys=keys).status == "invalid"
    # Signed by another trusted key than the one the payload names: refused, never "verified by whichever key".
    kid = statement["predicate"]["signing"]["key_id"]
    other = Ed25519PrivateKey.generate()
    both = parse_key_set(
        key_set_document(
            [*key_set["keys"], public_key_entry(other.public_key(), key_id="user-other", issuer=kid)], issuer=kid
        )
    )
    swapped = _resign(statement, Ed25519RecordSigner(other, key_id="user-other", mode="user_key"))
    result = verify_record(swapped, keys=both)
    assert result.status == "invalid" and "is not the key the payload names" in result.reasons[0]
    # The right key material published for another issuer never verifies this issuer's records.
    foreign = parse_key_set(
        key_set_document([public_key_entry(key.public_key(), key_id=kid, issuer="someone-else")], issuer="x")
    )
    result = verify_record(envelope, keys=foreign)
    assert result.status == "invalid" and "belongs to issuer" in result.reasons[0]
    assert verify_record(envelope, keys=None).status == "invalid"  # no trusted key
    schema = json.loads(json.dumps(statement))
    del schema["predicate"]["policy_generation"]
    assert verify_record(_resign(schema, _signer_for(statement, key)), keys=keys).status == "invalid"


def test_a_record_signed_after_revocation_fails_and_one_signed_before_stays_verifiable(
    signed_record: tuple[dict[str, Any], dict[str, Any], Ed25519PrivateKey],
) -> None:
    envelope, key_set, key = signed_record
    statement = _payload(envelope)
    issued = _instant(statement["predicate"]["issued_at"])
    entry = dict(key_set["keys"][0])

    revoked_later = {
        **entry,
        "status": "revoked",
        "revoked_at": (issued + timedelta(hours=1)).isoformat(),
        "revocation_reason": "laptop retired",
    }
    before = verify_record(envelope, keys=_keys(revoked_later))
    assert before.status == "verified" and before.exit_code == 0
    assert before.key_status == "revoked" and before.key_revoked_at  # verifiable, but shows the revocation

    revoked_earlier = {**revoked_later, "revoked_at": (issued - timedelta(seconds=1)).isoformat()}
    after = verify_record(envelope, keys=_keys(revoked_earlier))
    assert after.status == "revoked" and after.exit_code == EXIT_CODES["revoked"] == 3
    assert after.headline().startswith("REVOKED")

    revoked_at_issue = {**revoked_later, "revoked_at": issued.isoformat()}
    assert verify_record(envelope, keys=_keys(revoked_at_issue)).status == "revoked"

    # A record re-signed with the revoked key after its revocation fails too.
    late = _resign(_with_issued_at(statement, issued + timedelta(hours=2)), _signer_for(statement, key))
    assert verify_record(late, keys=_keys(revoked_later), now=issued + timedelta(hours=3)).status == "revoked"

    # Revoked without a time: compromise of unknown start revokes every record.
    unknown = {**entry, "status": "revoked", "revoked_at": ""}
    assert verify_record(envelope, keys=_keys(unknown)).status == "revoked"


def test_rotation_keeps_old_records_verifiable_and_retires_the_old_key(
    signed_record: tuple[dict[str, Any], dict[str, Any], Ed25519PrivateKey],
) -> None:
    envelope, key_set, key = signed_record
    statement = _payload(envelope)
    issued = _instant(statement["predicate"]["issued_at"])
    old_entry = {**key_set["keys"][0], "status": "retired", "not_after": (issued + timedelta(minutes=10)).isoformat()}
    new_key = ec.generate_private_key(ec.SECP256R1())
    new_entry = public_key_entry(
        new_key.public_key(),
        key_id="org-key-2",
        not_before=(issued + timedelta(minutes=10)).isoformat(),
        issuer=old_entry["issuer"],
    )
    keys = _keys(old_entry, new_entry)

    assert verify_record(envelope, keys=keys).status == "verified"  # the old record survives rotation

    rotated_statement = _with_issued_at(statement, issued + timedelta(minutes=20))
    rotated_statement["predicate"]["signing"] = {
        **rotated_statement["predicate"]["signing"],
        "mode": "organization_key",
        "key_id": "org-key-2",
        "algorithm": "ES256",
    }
    new_record = _resign(rotated_statement, _Es256Signer(new_key, "org-key-2"))
    assert verify_record(new_record, keys=keys, now=issued + timedelta(hours=1)).status == "verified"

    # The retired key no longer signs, and the new key did not sign before it existed.
    stale = _resign(_with_issued_at(statement, issued + timedelta(minutes=20)), _signer_for(statement, key))
    result = verify_record(stale, keys=keys, now=issued + timedelta(hours=1))
    assert result.status == "invalid" and "retired" in result.reasons[0]
    early_statement = json.loads(json.dumps(rotated_statement))
    early_statement["predicate"]["issued_at"] = early_statement["predicate"]["as_of"] = issued.isoformat()
    early = _resign(early_statement, _Es256Signer(new_key, "org-key-2"))
    assert verify_record(early, keys=keys).status == "invalid"

    # The record cannot claim to be from the future to dodge a revocation.
    future = _resign(_with_issued_at(rotated_statement, issued + timedelta(days=2)), _Es256Signer(new_key, "org-key-2"))
    assert verify_record(future, keys=keys, now=issued + timedelta(hours=1)).status == "invalid"


# --------------------------------------------------------------------------- #
# local personal key: explicit opt-in, never inside a repository
# --------------------------------------------------------------------------- #


def test_a_personal_key_never_lives_inside_a_repository(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    inside = repo_root / ".lemoncrow" / "user-key.pem"
    assert "inside the Git work tree" in refuse_key_location(inside)
    assert "inside the repository under review" in refuse_key_location(inside, repo_root=repo_root)
    with pytest.raises(RecordSigningUnavailable) as refused:
        generate_user_key(inside)
    assert refused.value.code == "forbidden" and not inside.exists()

    outside = tmp_path / "keys" / "user-key.pem"
    key_id = generate_user_key(outside)
    assert key_id.startswith("user-") and os.stat(outside).st_mode & 0o777 == 0o600
    with pytest.raises(RecordSigningUnavailable):
        generate_user_key(outside)  # never overwrites
    assert user_key_id(load_user_key(outside).public_key()) == key_id
    os.chmod(outside, 0o644)
    with pytest.raises(RecordSigningUnavailable) as loose:
        load_user_key(outside)
    assert loose.value.code == "forbidden"


def test_a_configured_key_inside_the_repository_refuses_auto_signing(record_workspace: RecordWorkspace) -> None:
    ws = record_workspace
    key_path = ws.settings_root / "elsewhere" / "k.pem"
    generate_user_key(key_path)
    planted = ws.workspace.repo_root / "k.pem"
    planted.write_bytes(key_path.read_bytes())
    os.chmod(planted, 0o600)
    write_setting(ws.settings_root, USER_KEY_SETTING, str(planted))
    ws.approve()
    status = ws.status()["signing"]
    assert status["available"] is False and status["code"] == "forbidden"
    refused = ws.export("auto")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "forbidden"


# --------------------------------------------------------------------------- #
# CLI: lc review verify-record / record-key
# --------------------------------------------------------------------------- #


def _cli(root: Path, *args: str) -> Any:
    return CliRunner().invoke(cli, ["--root", str(root), "review", *args], catch_exceptions=False)


def test_verify_record_cli_exit_codes_distinguish_verified_unsigned_invalid_and_revoked(
    record_workspace: RecordWorkspace, tmp_path: Path
) -> None:
    ws = record_workspace
    ws.opt_in()
    ws.approve()
    signed = ws.export().json()
    unsigned = ws.export("unsigned").json()
    key_set = _local_keys(ws)
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps(key_set))
    signed_file = tmp_path / "signed.json"
    signed_file.write_text(json.dumps(signed["envelope"]))
    unsigned_file = tmp_path / "unsigned.json"
    unsigned_file.write_text(json.dumps(unsigned["envelope"]))
    other_root = tmp_path / "other-root"

    verified = _cli(other_root, "verify-record", str(signed_file), "--keys", str(keys_file))
    assert verified.exit_code == 0, verified.output
    assert verified.output.startswith("VERIFIED") and signed["record"]["payload_digest"] in verified.output
    # The API export wrapper is accepted as well as the bare envelope.
    wrapper = tmp_path / "wrapper.json"
    wrapper.write_text(json.dumps(signed))
    assert _cli(other_root, "verify-record", str(wrapper), "--keys", str(keys_file)).exit_code == 0

    as_unsigned = _cli(other_root, "verify-record", str(unsigned_file), "--keys", str(keys_file))
    assert as_unsigned.exit_code == 2 and as_unsigned.output.startswith("UNSIGNED")

    tampered = dict(signed["envelope"])
    statement = _payload(tampered)
    statement["predicate"]["decision"]["outcome"] = "merged"
    tampered["payload"] = base64.b64encode(canonical_json(statement)).decode("ascii")
    tampered_file = tmp_path / "tampered.json"
    tampered_file.write_text(json.dumps(tampered))
    invalid = _cli(other_root, "verify-record", str(tampered_file), "--keys", str(keys_file))
    assert invalid.exit_code == 1 and invalid.output.startswith("INVALID")
    no_keys = _cli(other_root, "verify-record", str(signed_file))
    assert no_keys.exit_code == 1 and "--keys" in no_keys.output

    issued = _instant(_payload(signed["envelope"])["predicate"]["issued_at"])
    revoked = json.loads(json.dumps(key_set))
    revoked["keys"][0].update(status="revoked", revoked_at=(issued - timedelta(minutes=1)).isoformat())
    revoked_file = tmp_path / "revoked.json"
    revoked_file.write_text(json.dumps(revoked))
    as_revoked = _cli(other_root, "verify-record", str(signed_file), "--keys", str(revoked_file), "--json")
    assert as_revoked.exit_code == 3
    assert json.loads(as_revoked.output)["status"] == "revoked"

    # Default keys: the caller's own configured personal key.
    assert _cli(ws.settings_root, "verify-record", str(signed_file)).exit_code == 0
    not_json = tmp_path / "garbage.json"
    not_json.write_text("not json")
    assert _cli(other_root, "verify-record", str(not_json), "--keys", str(keys_file)).exit_code == 1
    assert _cli(other_root, "verify-record", str(signed_file), "--keys", "http://keys.invalid/k.json").exit_code == 1


def test_record_key_cli_opts_in_outside_any_repository(tmp_path: Path) -> None:
    root = tmp_path / "root"
    public_before = _cli(root, "record-key", "public")
    assert public_before.exit_code != 0 and "record-key init" in public_before.output

    repo_root = tmp_path / "repo"
    _init_repo(repo_root)
    refused = _cli(root, "record-key", "init", "--path", str(repo_root / "key.pem"))
    assert refused.exit_code != 0 and "Git work tree" in refused.output
    assert not (repo_root / "key.pem").exists()

    created = _cli(root, "record-key", "init")
    assert created.exit_code == 0, created.output
    key_path = root / "review-signing" / "user-key.pem"
    assert key_path.exists() and os.stat(key_path).st_mode & 0o777 == 0o600
    settings = json.loads((root / "plugin_settings.json").read_text())
    assert settings[USER_KEY_SETTING] == str(key_path.resolve())
    published = _cli(root, "record-key", "public")
    assert published.exit_code == 0
    document = json.loads(published.output)
    assert document["type"] == "lemoncrow.review-record-keys" and document["keys"][0]["kty"] == "OKP"
    assert "d" not in document["keys"][0]  # never the private half
    again = _cli(root, "record-key", "init")
    assert again.exit_code != 0 and "refusing to overwrite" in again.output


def test_cited_evidence_ids_reads_back_both_places_a_record_writes_evidence() -> None:
    """Trust incident runbook (Gate T-J): a v1 record names reused results only by evidence id."""

    from lemoncrow.pro.capabilities.review.review_record import cited_evidence_ids, readiness_section

    readiness = readiness_section(
        {
            "conditions": [
                {"id": "checks", "evidence_refs": [{"evidence_id": "ev-b"}, {"evidence_id": "ev-a"}, {"other": 1}]},
                {"id": "ci", "evidence_refs": [{"evidence_id": "ev-b"}]},
                "not a condition",
            ]
        }
    )
    statement = {"predicate": {"evidence": [{"id": "ev-a"}, {"id": "ev-c"}, {"id": ""}, "x"], "readiness": readiness}}

    assert cited_evidence_ids(statement) == {
        "ev-a": ("evidence", "condition:checks"),
        "ev-b": ("condition:checks", "condition:ci"),
        "ev-c": ("evidence",),
    }
    assert cited_evidence_ids({}) == {} and cited_evidence_ids({"predicate": "broken"}) == {}


# --------------------------------------------------------------------------- #
# P6-1: v2 sections (parts, AI hints, change) bound to a human record
# --------------------------------------------------------------------------- #


def _ai_hint(ws: RecordWorkspace, line: int) -> str:
    from lemoncrow.pro.capabilities.review.sources.local import annotate_machine_finding

    store = ws.workspace.store
    session = store.get_session(ws.review_id)
    revision = store.latest_revision(ws.review_id)
    assert session is not None and revision is not None
    annotation, _created = annotate_machine_finding(
        store,
        session,
        revision,
        path="src/session.py",
        start_line=line,
        end_line=line,
        body=f"hint on line {line}",
        title=f"hint {line}",
        created_by="investigator",
        created_by_actor="agent",
        source="ai_review",
        source_id=f"investigator-{line}",
        new_text=_CHANGED_SOURCE,
    )
    return annotation.id


def test_a_human_record_binds_parts_ai_hints_and_the_change_and_each_matches_the_store(
    record_workspace: RecordWorkspace,
) -> None:
    ws = record_workspace
    targets = ws.workspace.get(f"/api/reviews/{ws.review_id}/targets").json()
    target = targets["targets"][0]
    hint_id = _ai_hint(ws, 10)
    marked = ws.workspace.post(
        f"/api/reviews/{ws.review_id}/marks",
        json={"unit_key": target["unit_key"], "state": "needs_changes", "expected_revision_id": ws.latest_id()},
    )
    assert marked.status_code == 200, marked.text
    ws.opt_in()
    ws.approve(outcome="changes_requested")
    exported = ws.export()
    assert exported.status_code == 201, exported.text
    body = exported.json()
    statement = _payload(body["envelope"])
    predicate = statement["predicate"]
    assert statement["predicateType"] == PREDICATE_TYPE_V2 and predicate["schema_version"] == 2
    assert validate_statement(statement) == []
    # Parts: the same answer counts the Overview reports, and the one part answered.
    assert predicate["parts"]["counts"] == {**targets["answers"]}
    row = {"unit_key": target["unit_key"], "label": target["label"], "path": target["path"]}
    # This fixture's one part is a quick check marked Needs a change: listed, never a decision.
    assert target["answer"]["category"] == "quick_check"
    assert predicate["parts"]["decisions"] == [] and predicate["parts"]["quick_check_changes"] == [row]
    assert predicate["parts"]["later"] == 0
    # AI hints: the one raised on this revision, never marked as a decision.
    assert predicate["ai_hints"]["basis"] == "raised_on_revision_not_proof_shown"
    assert [hint["id"] for hint in predicate["ai_hints"]["hints"]] == [hint_id]
    assert (predicate["ai_hints"]["hints"][0]["start_line"], predicate["ai_hints"]["hints"][0]["end_line"]) == (10, 10)
    # Change: what the revision captured, certainty included (a working-tree capture names no agent).
    revision = ws.workspace.store.get_revision(ws.latest_id())
    assert revision is not None
    assert predicate["change"] == {
        "agent_host": revision.provenance_host,
        "agent_session_id": revision.provenance_session_id,
        "provenance_certainty": revision.provenance_certainty,
    }
    assert {key: body["store"][key] for key in ("parts", "ai_hints", "change", "seen_coverage")} == {
        "parts": "match",
        "ai_hints": "match",
        "change": "match",
        "seen_coverage": "match",
    }
    assert body["reproduced"] is True
    assert ws.status()["records"][0]["parts_reviewed_one_by_one"] == 0

    # An answer changed after signing mismatches the parts section only; the signature is untouched.
    unmarked = ws.workspace.post(
        f"/api/reviews/{ws.review_id}/marks",
        json={"unit_key": target["unit_key"], "state": "later", "expected_revision_id": ws.latest_id()},
    )
    assert unmarked.status_code == 200, unmarked.text
    again = ws.workspace.get(f"/api/reviews/{ws.review_id}/records/{body['record']['record_id']}/verify").json()
    assert again["verification"]["status"] == "verified"
    assert again["store"]["parts"] == "mismatch"
    assert again["store"]["ai_hints"] == "match" and again["store"]["change"] == "match"


def test_parts_section_counts_each_category_and_names_only_what_was_not_confirmed() -> None:
    from lemoncrow.pro.capabilities.review.review_record import RecordPart, parts_section

    def part(key: str, category: str, state: str, rule: str = "") -> RecordPart:
        return RecordPart(unit_key=key, label=key, path=f"{key}.py", category=category, state=state, rule=rule)

    section = parts_section(
        [
            part("a", "decision", "reviewed"),
            part("b", "decision", "needs_changes"),
            part("c", "decision", "later"),
            part("d", "decision", "changed_since_review"),
            part("e", "quick_check", "reviewed"),
            part("f", "quick_check", "unreviewed"),
            part("g", "quick_check", "needs_changes"),
            part("h", "quick_check", "later"),
            part("i", "accepted_by_rule", "unreviewed", rule="lockfiles"),
            part("j", "accepted_by_rule", "reviewed", rule="generated_files"),
        ]
    )
    assert section["counts"] == {
        "decision": 4,
        "quick_check": 4,
        "accepted_by_rule": 2,
        "rules": {"generated_files": 1, "lockfiles": 1},
    }
    assert [(row["unit_key"], row["state"]) for row in section["decisions"]] == [
        ("a", "reviewed"),
        ("b", "needs_changes"),
        ("c", "later"),
        ("d", "unanswered"),
    ]
    assert [row["unit_key"] for row in section["quick_check_changes"]] == ["g"]
    assert [row["unit_key"] for row in section["not_confirmed"]] == ["f"]
    assert section["later"] == 2
    # Accepted-by-rule parts are counted with their rule and listed nowhere else.
    listed = {row["unit_key"] for key in ("decisions", "quick_check_changes", "not_confirmed") for row in section[key]}
    assert "i" not in listed and "j" not in listed


def test_a_landing_record_never_carries_the_v2_sections(record_workspace: RecordWorkspace) -> None:
    ws = record_workspace
    ws.opt_in()
    from lemoncrow.pro.capabilities.review.review_record import ReviewDecision

    decision = ReviewDecision(
        kind="landing",
        outcome="auto_approved",
        actor_id="lemoncrow-auto-approval",
        actor_type="system",
        decided_at="2026-11-07T09:00:00+00:00",
        reference="rel-1",
    )
    record = ws.app.state.review_record_signer(ws.review_id, signing="auto", decision=decision)
    statement = _payload(record.envelope)
    assert statement["predicateType"] == PREDICATE_TYPE and statement["predicate"]["schema_version"] == 1
    assert not {"parts", "ai_hints", "change", "seen_coverage"} & set(statement["predicate"])


def _verify_outcome(ws: RecordWorkspace, record_id: str) -> dict[str, Any]:
    response = ws.workspace.get(f"/api/reviews/{ws.review_id}/records/{record_id}/verify")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_the_verify_route_names_valid_then_code_changed_when_a_newer_revision_is_captured(
    record_workspace: RecordWorkspace,
) -> None:
    ws = record_workspace
    ws.opt_in()
    ws.approve()
    record_id = ws.export().json()["record"]["record_id"]
    valid = _verify_outcome(ws, record_id)
    assert valid["outcome"]["outcome"] == "valid" and valid["outcome"]["code"] == "matches"
    assert valid["current"]["revision_number"] == valid["record"]["snapshot_number"]
    # What the dialog's footer and Details read: the signed repository and the plain rows.
    assert valid["repository"] == _payload(ws.export().json()["envelope"])["predicate"]["review"]["repository"]
    assert {"Version", "Your verdict", "Signed at", "Signed by"} <= {row["label"] for row in valid["details"]}

    _write(ws.workspace.repo_root, "src/session.py", _CHANGED_SOURCE + "\n# the agent kept going\n")
    rng = resolve_rev_range(ws.workspace.repo_root, None, working_tree=True)
    build = build_review_packet_with_blobs(
        ws.workspace.repo_root, rng, store_root=ws.workspace.store_root, with_patch_text=True
    )
    session = ws.workspace.store.get_session(ws.review_id)
    assert session is not None
    snapshot_revision(
        ws.workspace.store, session, ws.workspace.repo_root, rng, store_root=ws.workspace.store_root, build=build
    )
    changed = _verify_outcome(ws, record_id)
    assert changed["outcome"]["outcome"] == "code_changed" and changed["outcome"]["exit_code"] == 4
    # The signature is untouched by the newer code: only the code check moved.
    assert changed["verification"]["status"] == "verified"


def test_the_verify_route_names_key_not_allowed_when_the_signing_key_is_replaced(
    record_workspace: RecordWorkspace,
) -> None:
    ws = record_workspace
    ws.opt_in()
    ws.approve()
    record_id = ws.export().json()["record"]["record_id"]
    second = ws.settings_root / "review-signing" / "second-key.pem"
    generate_user_key(second)
    write_setting(ws.settings_root, USER_KEY_SETTING, str(second))
    outcome = _verify_outcome(ws, record_id)["outcome"]
    assert (outcome["outcome"], outcome["key_problem"], outcome["exit_code"]) == ("key_not_allowed", "not_trusted", 3)


def test_a_stored_v1_human_record_and_a_p3_2_era_record_still_reproduce_from_the_store(
    record_workspace: RecordWorkspace,
) -> None:
    """The rebuild keeps the version and sections of the record it reproduces (`shape=`), so older records still match."""

    from lemoncrow.pro.capabilities.review.record_signing import (
        SignedReviewRecord,
        persist_record,
        record_id_for,
    )

    ws = record_workspace
    key_path, key_id = ws.opt_in()
    ws.approve()
    exported = ws.export().json()
    statement = _payload(exported["envelope"])
    assert exported["reproduced"] is True
    signer = Ed25519RecordSigner(
        load_user_key(key_path), key_id=key_id, mode="user_key", issuer_kind="local", issuer_id=key_id
    )

    def stored(mutate: Any) -> str:
        copy = json.loads(json.dumps(statement))
        mutate(copy)
        assert validate_statement(copy) == []
        payload = canonical_json(copy)
        digest = sha256_digest(payload)
        record = SignedReviewRecord(
            record_id=record_id_for(digest),
            review_id=ws.review_id,
            revision_id=ws.latest_id(),
            payload_digest=digest,
            signing_state="signed",
            signing_mode="user_key",
            key_id=key_id,
            issued_at=copy["predicate"]["issued_at"],
            decision_kind="human_outcome",
            envelope=dsse_envelope(payload, signer),
            statement=copy,
        )
        persist_record(ws.workspace.store, record)
        return record.record_id

    def as_v1(copy: dict[str, Any]) -> None:
        copy["predicateType"] = PREDICATE_TYPE
        copy["predicate"]["schema_version"] = 1
        for key in ("seen_coverage", "parts", "ai_hints", "change"):
            copy["predicate"].pop(key, None)

    def as_p3_2_era(copy: dict[str, Any]) -> None:
        for key in ("parts", "ai_hints", "change"):
            copy["predicate"].pop(key, None)

    for name, mutate in (("v1", as_v1), ("p3_2_era", as_p3_2_era)):
        verified = _verify_outcome(ws, stored(mutate))
        assert verified["verification"]["status"] == "verified", name
        assert verified["reproduced"] is True, f"{name}: the rebuilt payload must equal the stored one"
