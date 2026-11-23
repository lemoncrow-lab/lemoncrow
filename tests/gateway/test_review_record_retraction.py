"""P6-2: a retraction is a second statement signed by the key that signed the record, inside 24 hours of signing.

The pure rules (the window to the microsecond, the same-key rule, who may retract, what the verifier accepts) are
checked on real signed envelopes; the server's routes are in ``test_review_record_retract_api``.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lemoncrow.pro.capabilities.review.record_outcome import CurrentCode, record_outcome
from lemoncrow.pro.capabilities.review.record_retraction import (
    RETRACT_WINDOW,
    RetractionRefused,
    build_retraction_statement,
    retract_state,
    retract_until,
    sign_retraction,
    window_open,
)
from lemoncrow.pro.capabilities.review.record_signing import (
    Ed25519RecordSigner,
    SignedReviewRecord,
    dsse_envelope,
    key_set_document,
    public_key_entry,
    sign_review_record,
)
from lemoncrow.pro.capabilities.review.record_verify import PublishedKey, parse_key_set
from lemoncrow.pro.capabilities.review.review_record import ReviewDecision, canonical_json
from tests.gateway.test_review_record_golden import ISSUED_AT, KEY_ID, _context, _decisions, _signer

SIGNED = datetime.fromisoformat(ISSUED_AT)
NOW = SIGNED + timedelta(days=3)
OTHER_KEY_ID = "user-golden-0002"


def _at(delta: timedelta) -> str:
    return (SIGNED + delta).astimezone(UTC).isoformat()


def _verdict(actor: str = "srivera") -> ReviewDecision:
    return ReviewDecision(
        kind="human_outcome",
        outcome="approved",
        actor_id=actor,
        actor_type="human",
        decided_at=ISSUED_AT,
        reference="out-1",
    )


def _record(tmp_path: Path, decision: ReviewDecision | None = None, *, signed: bool = True) -> SignedReviewRecord:
    return sign_review_record(
        _context(tmp_path),
        decision or _verdict(),
        signer=_signer() if signed else None,
        issued_at=ISSUED_AT,
        persist=False,
    )


def _other_signer() -> Ed25519RecordSigner:
    return Ed25519RecordSigner(
        Ed25519PrivateKey.from_private_bytes(bytes([9]) * 32), key_id=OTHER_KEY_ID, mode="user_key"
    )


def _keys(*signers: Ed25519RecordSigner, **lifetime: str) -> dict[str, PublishedKey]:
    entries = [
        public_key_entry(s._private_key.public_key(), key_id=s.identity.key_id, issuer=s.identity.key_id, **lifetime)
        for s in signers
    ]
    return parse_key_set(key_set_document(entries, issuer=KEY_ID))


def _retract(record: SignedReviewRecord, delta: timedelta, **kwargs: Any) -> dict[str, Any]:
    return dict(
        sign_retraction(
            record,
            kwargs.pop("signer", _signer()),
            retracted_by=kwargs.pop("by", "srivera"),
            retracted_by_type="human",
            retracted_at=_at(delta),
            **kwargs,
        ).envelope
    )


def _outcome(record: SignedReviewRecord, retraction: object, **kwargs: Any) -> Any:
    kwargs.setdefault("keys", _keys(_signer()))
    return record_outcome(
        record.envelope,
        current=CurrentCode(tree_fingerprint="tree-" + "a" * 32, head_sha="c" * 40),
        retraction_document=retraction,
        now=NOW,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# signing and the window
# --------------------------------------------------------------------------- #


def test_a_retraction_inside_the_window_makes_the_record_retracted(tmp_path: Path) -> None:
    record = _record(tmp_path)
    result = _outcome(record, _retract(record, timedelta(hours=1)))
    assert (result.outcome, result.exit_code) == ("retracted", 5)
    assert result.headline() == "Retracted · by @srivera at 10:00 UTC"
    assert result.to_wire()["retraction"] == {"by": "srivera", "at": _at(timedelta(hours=1))}


def test_the_record_itself_is_unchanged_and_still_verifies_without_the_retraction(tmp_path: Path) -> None:
    record = _record(tmp_path)
    before = json.dumps(dict(record.envelope), sort_keys=True)
    _retract(record, timedelta(hours=1))
    assert json.dumps(dict(record.envelope), sort_keys=True) == before
    assert _outcome(record, None).outcome == "valid"


def test_the_window_is_signed_time_plus_24_hours_and_the_end_itself_is_closed() -> None:
    day = timedelta(hours=24)
    assert retract_until(ISSUED_AT) == SIGNED + day
    just_before = _at(day - timedelta(microseconds=1))
    assert window_open(ISSUED_AT, just_before)
    assert not window_open(ISSUED_AT, _at(day))
    assert not window_open(ISSUED_AT, _at(day + timedelta(microseconds=1)))
    assert window_open(ISSUED_AT, ISSUED_AT)


def test_signing_a_retraction_at_the_boundary(tmp_path: Path) -> None:
    record = _record(tmp_path)
    day = timedelta(hours=24)
    _retract(record, day - timedelta(microseconds=1))  # the last instant that works
    for late in (day, day + timedelta(microseconds=1), day + timedelta(hours=5)):
        with pytest.raises(RetractionRefused) as refused:
            _retract(record, late)
        assert refused.value.code == "window_closed"
        assert refused.value.retract_until == _at(day)


def test_a_tighter_window_closes_sooner_and_can_never_be_widened(tmp_path: Path) -> None:
    record = _record(tmp_path)
    assert retract_until(ISSUED_AT, timedelta(seconds=5)) == SIGNED + timedelta(seconds=5)
    assert retract_until(ISSUED_AT, timedelta(days=30)) == SIGNED + RETRACT_WINDOW
    with pytest.raises(RetractionRefused):
        _retract(record, timedelta(seconds=5), window=timedelta(seconds=5))
    _retract(record, timedelta(seconds=4), window=timedelta(seconds=5))
    with pytest.raises(RetractionRefused):
        _retract(record, timedelta(hours=25), window=timedelta(days=30))


def test_the_window_state_is_open_closed_or_retracted(tmp_path: Path) -> None:
    record = _record(tmp_path)
    retraction = sign_retraction(
        record, _signer(), retracted_by="srivera", retracted_by_type="human", retracted_at=_at(timedelta(hours=2))
    )
    assert retract_state(record, None, _at(timedelta(hours=2))).state == "open"
    assert retract_state(record, None, _at(timedelta(hours=24))).state == "closed"
    shown = retract_state(record, retraction, _at(timedelta(hours=30)))
    assert (shown.state, shown.retracted_by, shown.until) == ("retracted", "srivera", _at(timedelta(hours=24)))
    assert retract_state(record, None, ISSUED_AT, timedelta(seconds=5)).full_window is False


# --------------------------------------------------------------------------- #
# who and with what
# --------------------------------------------------------------------------- #


def test_only_a_verdict_a_person_sent_can_be_retracted(tmp_path: Path) -> None:
    landing = _record(tmp_path, _decisions()["landing"])
    with pytest.raises(RetractionRefused) as refused:
        _retract(landing, timedelta(hours=1))
    assert refused.value.code == "not_a_verdict"


def test_an_unsigned_record_cannot_be_retracted(tmp_path: Path) -> None:
    with pytest.raises(RetractionRefused) as refused:
        _retract(_record(tmp_path, signed=False), timedelta(hours=1))
    assert refused.value.code == "unsigned"


def test_the_key_that_signed_the_record_is_the_only_key_that_can_retract_it(tmp_path: Path) -> None:
    record = _record(tmp_path)
    with pytest.raises(RetractionRefused) as refused:
        _retract(record, timedelta(hours=1), signer=_other_signer())
    assert refused.value.code == "different_key"


def test_a_retraction_signed_by_another_trusted_key_is_not_valid(tmp_path: Path) -> None:
    """A key the verifier trusts is still not the record's key: the signing step is bypassed to forge one."""

    record = _record(tmp_path)
    other = _other_signer()
    forged = build_retraction_statement(
        record,
        retracted_by="srivera",
        retracted_by_type="human",
        retracted_at=_at(timedelta(hours=1)),
        key_id=OTHER_KEY_ID,
        algorithm="Ed25519",
        mode="user_key",
    )
    result = _outcome(record, dict(dsse_envelope(canonical_json(forged), other)), keys=_keys(_signer(), other))
    assert result.outcome == "not_valid"
    assert result.headline() == "Not valid · the retraction was signed by a different key than the record"


# --------------------------------------------------------------------------- #
# what the offline verifier accepts
# --------------------------------------------------------------------------- #


def _forged(record: SignedReviewRecord, retracted_at: str, **changes: Any) -> dict[str, Any]:
    """A retraction signed with the right key whose claims the signing step would have refused."""

    statement = build_retraction_statement(
        record,
        retracted_by=changes.pop("by", "srivera"),
        retracted_by_type="human",
        retracted_at=retracted_at,
        key_id=KEY_ID,
        algorithm="Ed25519",
        mode="user_key",
    )
    statement["predicate"].update(changes)
    return dict(dsse_envelope(canonical_json(statement), _signer()))


def test_the_verifier_holds_a_retraction_to_the_same_window(tmp_path: Path) -> None:
    record = _record(tmp_path)
    day = timedelta(hours=24)
    assert _outcome(record, _forged(record, _at(day - timedelta(microseconds=1)))).outcome == "retracted"
    for late in (day, day + timedelta(microseconds=1)):
        result = _outcome(record, _forged(record, _at(late)))
        assert result.outcome == "not_valid"
        assert result.headline() == "Not valid · the retraction was made after the 24 hours from signing"


def test_a_retraction_cannot_claim_a_longer_window_than_it_signed(tmp_path: Path) -> None:
    record = _record(tmp_path)
    claimed = _forged(record, _at(timedelta(hours=2)), retract_until=_at(timedelta(hours=1)))
    assert _outcome(record, claimed).outcome == "not_valid"


def test_a_retraction_dated_before_the_record_or_in_the_future_is_not_valid(tmp_path: Path) -> None:
    record = _record(tmp_path)
    assert _outcome(record, _forged(record, _at(-timedelta(seconds=1)))).outcome == "not_valid"
    future = record_outcome(
        record.envelope,
        keys=_keys(_signer()),
        current=None,
        retraction_document=_forged(record, _at(timedelta(hours=1))),
        now=SIGNED,
    )
    assert future.outcome == "not_valid"


def test_a_retraction_by_someone_other_than_who_decided_is_not_valid(tmp_path: Path) -> None:
    record = _record(tmp_path)
    result = _outcome(record, _forged(record, _at(timedelta(hours=1)), by="mchen"))
    assert result.outcome == "not_valid"


def test_a_retraction_of_another_record_is_not_valid(tmp_path: Path) -> None:
    """Another verdict of the same person on the same revision: really a different record, its own real retraction."""

    record = _record(tmp_path)
    other_decision = ReviewDecision(
        kind="human_outcome",
        outcome="changes_requested",
        actor_id="srivera",
        actor_type="human",
        decided_at=ISSUED_AT,
        reference="out-2",
    )
    other = _record(tmp_path, other_decision)
    assert other.payload_digest != record.payload_digest
    other_retraction = _retract(other, timedelta(hours=1))
    assert _outcome(other, other_retraction).outcome == "retracted"
    swapped = _outcome(record, other_retraction)
    assert swapped.outcome == "not_valid"
    assert swapped.headline() == "Not valid \u00b7 the retraction is for a different record"


def test_an_altered_retraction_is_not_valid(tmp_path: Path) -> None:
    record = _record(tmp_path)
    envelope = _retract(record, timedelta(hours=1))
    envelope["signatures"][0]["sig"] = base64.b64encode(b"x" * 64).decode("ascii")
    result = _outcome(record, envelope)
    assert result.outcome == "not_valid"
    assert result.headline() == "Not valid · the retraction has a signature that is not valid"


def test_a_retraction_that_is_not_a_retraction_is_not_valid(tmp_path: Path) -> None:
    record = _record(tmp_path)
    assert _outcome(record, {"hello": "world"}).outcome == "not_valid"
    assert _outcome(record, dict(record.envelope)).outcome == "not_valid"


def test_a_retraction_after_the_key_was_revoked_is_not_valid(tmp_path: Path) -> None:
    record = _record(tmp_path)
    envelope = _retract(record, timedelta(hours=3))
    revoked = _keys(_signer(), status="revoked", revoked_at=_at(timedelta(hours=2)))
    # The record was signed before the revocation (still a valid record) but the retraction was not.
    result = _outcome(record, envelope, keys=revoked)
    assert result.outcome == "not_valid" and "revoked" in result.headline()
    early = _keys(_signer(), status="revoked", revoked_at=_at(timedelta(hours=4)))
    assert _outcome(record, envelope, keys=early).outcome == "retracted"


@pytest.mark.parametrize(
    ("record_kwargs", "expected"),
    [({"signed": False}, "unsigned"), ({"keys": {}}, "key_not_allowed")],
)
def test_a_retraction_never_rescues_a_record_that_cannot_be_verified(
    tmp_path: Path, record_kwargs: dict[str, Any], expected: str
) -> None:
    record = _record(tmp_path)
    unsigned = _record(tmp_path, signed=False)
    target = unsigned if record_kwargs.pop("signed", True) is False else record
    assert _outcome(target, _retract(record, timedelta(hours=1)), **record_kwargs).outcome == expected


def test_a_retraction_ranks_above_code_changed(tmp_path: Path) -> None:
    record = _record(tmp_path)
    moved = record_outcome(
        record.envelope,
        keys=_keys(_signer()),
        current=CurrentCode(tree_fingerprint="tree-other"),
        retraction_document=_retract(record, timedelta(hours=1)),
        now=NOW,
    )
    assert moved.outcome == "retracted"


# --------------------------------------------------------------------------- #
# v1 records keep verifying
# --------------------------------------------------------------------------- #


def test_a_v1_landing_record_still_verifies_with_no_retraction(tmp_path: Path) -> None:
    landing = _record(tmp_path, _decisions()["landing"])
    assert _outcome(landing, None).outcome == "valid"
    assert retract_state(landing, None, ISSUED_AT).state == "open"  # the pure window; the route never offers it
