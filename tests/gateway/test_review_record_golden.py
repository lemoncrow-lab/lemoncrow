"""P6-1: v1 records stay byte-identical.

A landing decision and a provider merge are never given the v2 sections, so the
bytes the builder writes for them (and the signature over them) are pinned by a
fixture generated before v2 existed. A change to the v1 builder breaks this test;
a deliberate v1 change needs a new predicate version, not an edited fixture.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lemoncrow.pro.capabilities.review.record_signing import (
    Ed25519RecordSigner,
    key_set_document,
    public_key_entry,
    sign_review_record,
)
from lemoncrow.pro.capabilities.review.record_verify import parse_key_set, verify_record
from lemoncrow.pro.capabilities.review.review_record import (
    PREDICATE_TYPE,
    PREDICATE_TYPE_V2,
    RecordContext,
    ReviewDecision,
    validate_statement,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import ReviewStore

FIXTURE = Path(__file__).parents[1] / "fixtures" / "review_record" / "v1_landing_golden.json"
ISSUED_AT = "2026-11-07T09:00:00+00:00"
KEY_ID = "user-golden-0001"


def _signer() -> Ed25519RecordSigner:
    return Ed25519RecordSigner(Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32), key_id=KEY_ID, mode="user_key")


def _context(tmp_path: Path) -> RecordContext:
    session = ReviewSession(
        id="0" * 31 + "1",
        subject_type="local_diff",
        repo_root="/not/in/the/record",
        range_mode="working_tree",
        title="Golden change",
        source_ref="main",
        created_at=ISSUED_AT,
    )
    revision = ReviewRevision(
        id="0" * 31 + "2",
        review_id=session.id,
        revision_number=1,
        range_mode="working_tree",
        tree_fingerprint="tree-" + "a" * 32,
        packet_schema_version=1,
        base_sha="b" * 40,
        head_sha="c" * 40,
        merge_base_sha="b" * 40,
        packet_sha256="d" * 64,
        source_fingerprint="e" * 32,
        created_at=ISSUED_AT,
    )
    readiness: dict[str, Any] = {
        "ready": True,
        "armable": False,
        "blocker_count": 0,
        "conditions_hash": "",
        "evaluated_at": ISSUED_AT,
        "valid_until": "",
        "conditions": [],
        "policy_generation": {"digest": "policy:" + "1" * 64, "authority": "local", "layers": []},
    }
    return RecordContext(
        store=ReviewStore(tmp_path / "store"),
        session=session,
        revision=revision,
        snapshot_number=1,
        readiness=readiness,
        run_plan=None,
        issuer_kind="local",
        issuer_id=KEY_ID,
        repository="golden/repo",
        exported_by_id="local",
    )


def _decisions() -> dict[str, ReviewDecision]:
    return {
        "landing": ReviewDecision(
            kind="landing",
            outcome="auto_approved",
            actor_id="lemoncrow-auto-approval",
            actor_type="system",
            decided_at=ISSUED_AT,
            reference="rel-landing-1",
            detail={"decision_digest": "sha256:" + "f" * 64},
        ),
        "provider_merge": ReviewDecision(
            kind="provider_merge",
            outcome="merged",
            actor_id="github",
            actor_type="system",
            decided_at=ISSUED_AT,
            reference="rel-merge-1",
            detail={"mechanism": "squash"},
        ),
    }


def build_golden(tmp_path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    signer = _signer()
    for name, decision in _decisions().items():
        record = sign_review_record(_context(tmp_path), decision, signer=signer, issued_at=ISSUED_AT, persist=False)
        out[name] = {"payload_digest": record.payload_digest, "envelope": dict(record.envelope)}
    public = signer._private_key.public_key()
    out["keys"] = key_set_document([public_key_entry(public, key_id=KEY_ID, issuer=KEY_ID)], issuer=KEY_ID)
    return out


@pytest.mark.parametrize("name", ["landing", "provider_merge"])
def test_the_v1_builder_still_writes_the_pinned_bytes(tmp_path: Path, name: str) -> None:
    golden = json.loads(FIXTURE.read_text())
    built = build_golden(tmp_path)
    assert built[name]["envelope"]["payload"] == golden[name]["envelope"]["payload"]
    assert built[name]["envelope"] == golden[name]["envelope"]
    assert built[name]["payload_digest"] == golden[name]["payload_digest"]


@pytest.mark.parametrize("name", ["landing", "provider_merge"])
def test_the_pinned_v1_records_still_verify(name: str) -> None:
    golden = json.loads(FIXTURE.read_text())
    statement = json.loads(base64.b64decode(golden[name]["envelope"]["payload"]))
    assert statement["predicateType"] == PREDICATE_TYPE and statement["predicate"]["schema_version"] == 1
    assert validate_statement(statement) == []
    verdict = verify_record(
        golden[name]["envelope"], keys=parse_key_set(golden["keys"]), now=datetime(2026, 11, 8, tzinfo=UTC)
    )
    assert verdict.status == "verified", verdict.reasons


def test_a_record_signed_in_the_p3_2_era_still_verifies_and_stays_valid_under_v2_rules() -> None:
    """A v2 record that has seen coverage and none of the P6-1 sections."""

    fixture = json.loads((FIXTURE.parent / "v2_p3_2_era.json").read_text())
    statement = json.loads(base64.b64decode(fixture["envelope"]["payload"]))
    assert statement["predicateType"] == PREDICATE_TYPE_V2 and statement["predicate"]["schema_version"] == 2
    assert not {"parts", "ai_hints", "change"} & set(statement["predicate"])
    assert validate_statement(statement) == []
    verdict = verify_record(
        fixture["envelope"], keys=parse_key_set(fixture["keys"]), now=datetime(2026, 11, 8, tzinfo=UTC)
    )
    assert verdict.status == "verified", verdict.reasons


def test_a_v2_record_with_a_malformed_new_section_is_invalid() -> None:
    fixture = json.loads((FIXTURE.parent / "v2_p3_2_era.json").read_text())
    statement = json.loads(base64.b64decode(fixture["envelope"]["payload"]))
    statement["predicate"]["parts"] = {"counts": {}}
    assert any("v2 parts" in error for error in validate_statement(statement))
    del statement["predicate"]["parts"]
    statement["predicate"]["ai_hints"] = {"basis": "something else", "hints": []}
    assert any("AI hints" in error for error in validate_statement(statement))
    del statement["predicate"]["ai_hints"]
    statement["predicate"]["change"] = {"agent_host": 3}
    assert any("v2 change" in error for error in validate_statement(statement))
