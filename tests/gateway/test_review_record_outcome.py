"""P6-1: the four outcomes the Verify dialog names, and their order of precedence.

Each test makes one real record (signed with a fixed key at build time) and breaks
exactly one thing: the payload, the trusted key set, the key's lifetime, the code
that was signed, or the record's standing.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.record_outcome import (
    CurrentCode,
    RetractionFact,
    code_check,
    record_outcome,
)
from lemoncrow.pro.capabilities.review.record_signing import (
    dsse_envelope,
    key_set_document,
    public_key_entry,
    sign_review_record,
)
from lemoncrow.pro.capabilities.review.record_verify import PublishedKey, parse_key_set
from lemoncrow.pro.capabilities.review.review_record import canonical_json
from tests.gateway.test_review_record_golden import ISSUED_AT, KEY_ID, _context, _decisions, _signer

NOW = datetime(2026, 11, 8, tzinfo=UTC)
SIGNED_TREE = "tree-" + "a" * 32


def _record(tmp_path: Path, *, signed: bool = True) -> dict[str, Any]:
    record = sign_review_record(
        _context(tmp_path),
        _decisions()["landing"],
        signer=_signer() if signed else None,
        issued_at=ISSUED_AT,
        persist=False,
    )
    return dict(record.envelope)


def _keys(**lifetime: str) -> dict[str, PublishedKey]:
    public = _signer()._private_key.public_key()
    return parse_key_set(
        key_set_document([public_key_entry(public, key_id=KEY_ID, issuer=KEY_ID, **lifetime)], issuer=KEY_ID)
    )


def _outcome(envelope: dict[str, Any], **kwargs: Any) -> Any:
    kwargs.setdefault("keys", _keys())
    kwargs.setdefault("current", CurrentCode(tree_fingerprint=SIGNED_TREE, head_sha="c" * 40))
    return record_outcome(envelope, now=NOW, **kwargs)


def test_a_record_over_the_same_code_is_valid(tmp_path: Path) -> None:
    result = _outcome(_record(tmp_path))
    assert (result.outcome, result.code, result.exit_code) == ("valid", "matches", 0)
    assert result.signed_head_sha == "c" * 40 and result.key_id == KEY_ID and result.signed_at == ISSUED_AT


def test_a_record_with_nothing_to_compare_is_valid_but_says_the_code_was_not_checked(tmp_path: Path) -> None:
    result = _outcome(_record(tmp_path), current=None)
    assert (result.outcome, result.code) == ("valid", "not_checked")


def test_code_that_moved_after_signing_is_code_changed(tmp_path: Path) -> None:
    result = _outcome(
        _record(tmp_path), current=CurrentCode(tree_fingerprint="tree-other", head_sha="8c3d0a1" + "0" * 33)
    )
    assert (result.outcome, result.code, result.exit_code) == ("code_changed", "changed", 4)
    assert result.current_head_sha.startswith("8c3d0a1") and result.signed_head_sha == "c" * 40


def test_a_key_that_is_not_trusted_is_key_not_allowed(tmp_path: Path) -> None:
    result = _outcome(_record(tmp_path), keys={})
    assert (result.outcome, result.key_problem, result.exit_code) == ("key_not_allowed", "not_trusted", 3)
    assert result.key_id == KEY_ID


def test_no_key_set_at_all_is_key_not_allowed(tmp_path: Path) -> None:
    assert _outcome(_record(tmp_path), keys=None).outcome == "key_not_allowed"


def test_a_key_revoked_before_the_record_was_issued_is_key_not_allowed(tmp_path: Path) -> None:
    keys = _keys(status="revoked", revoked_at="2026-11-06T00:00:00+00:00")
    result = _outcome(_record(tmp_path), keys=keys)
    assert (result.outcome, result.key_problem) == ("key_not_allowed", "revoked")


def test_a_key_revoked_after_the_record_was_issued_leaves_it_valid(tmp_path: Path) -> None:
    keys = _keys(status="revoked", revoked_at="2026-11-07T10:00:00+00:00")
    assert _outcome(_record(tmp_path), keys=keys).outcome == "valid"


def test_a_payload_altered_after_signing_is_not_valid_not_key_not_allowed(tmp_path: Path) -> None:
    envelope = _record(tmp_path)
    statement = json.loads(base64.b64decode(envelope["payload"]))
    statement["predicate"]["decision"]["outcome"] = "break_glass"
    envelope["payload"] = base64.b64encode(canonical_json(statement)).decode("ascii")
    result = _outcome(envelope)
    assert (result.outcome, result.key_problem, result.exit_code) == ("not_valid", "", 1)
    assert any("does not match the payload" in reason for reason in result.reasons)


def test_a_signature_by_a_different_key_with_the_same_id_is_not_valid(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    other = Ed25519PrivateKey.from_private_bytes(bytes([9]) * 32)
    keys = parse_key_set(
        key_set_document([public_key_entry(other.public_key(), key_id=KEY_ID, issuer=KEY_ID)], issuer=KEY_ID)
    )
    assert _outcome(_record(tmp_path), keys=keys).outcome == "not_valid"


def test_an_unsigned_record_is_unsigned(tmp_path: Path) -> None:
    result = _outcome(_record(tmp_path, signed=False))
    assert (result.outcome, result.exit_code) == ("unsigned", 2)


def test_a_retraction_makes_a_valid_record_retracted(tmp_path: Path) -> None:
    result = _outcome(_record(tmp_path), retraction=RetractionFact(by="srivera", at="2026-11-07T15:10:00+00:00"))
    assert (result.outcome, result.exit_code) == ("retracted", 5)
    assert result.to_wire()["retraction"] == {"by": "srivera", "at": "2026-11-07T15:10:00+00:00"}


@pytest.mark.parametrize(
    ("record_kwargs", "expected"),
    [
        ({"signed": False}, "unsigned"),
        ({"keys": {}}, "key_not_allowed"),
        ({"current": CurrentCode(tree_fingerprint="tree-other")}, "retracted"),
    ],
)
def test_a_retraction_ranks_below_unsigned_and_key_problems_and_above_code_changed(
    tmp_path: Path, record_kwargs: dict[str, Any], expected: str
) -> None:
    signed = record_kwargs.pop("signed", True)
    result = _outcome(
        _record(tmp_path, signed=signed),
        retraction=RetractionFact(by="srivera", at="2026-11-07T15:10:00+00:00"),
        **record_kwargs,
    )
    assert result.outcome == expected


def test_an_altered_retracted_record_is_not_valid(tmp_path: Path) -> None:
    envelope = _record(tmp_path)
    envelope["signatures"][0]["sig"] = base64.b64encode(b"x" * 64).decode("ascii")
    result = _outcome(envelope, retraction=RetractionFact(by="srivera", at="2026-11-07T15:10:00+00:00"))
    assert result.outcome == "not_valid"


def test_a_re_signed_payload_by_a_trusted_key_still_checks_the_code(tmp_path: Path) -> None:
    envelope = _record(tmp_path)
    statement = json.loads(base64.b64decode(envelope["payload"]))
    statement["predicate"]["snapshot"]["tree_fingerprint"] = "tree-moved"
    resigned = dsse_envelope(canonical_json(statement), _signer())
    assert _outcome(resigned).outcome == "code_changed"


@pytest.mark.parametrize(
    ("snapshot", "current", "expected"),
    [
        ({"tree_fingerprint": "t1", "head_sha": "h1", "dirty": False}, CurrentCode("t1", "h2"), "matches"),
        ({"tree_fingerprint": "t1", "head_sha": "h1", "dirty": False}, CurrentCode("t2", "h1"), "changed"),
        ({"tree_fingerprint": "t1", "head_sha": "h1", "dirty": False}, None, "not_checked"),
        ({"tree_fingerprint": "t1", "head_sha": "h1", "dirty": False}, CurrentCode("", "h2"), "changed"),
        ({"tree_fingerprint": "t1", "head_sha": "h1", "dirty": False}, CurrentCode("", "h1"), "matches"),
        ({"tree_fingerprint": "t1", "head_sha": "h1", "dirty": True}, CurrentCode("", "h2"), "not_checked"),
        ({"tree_fingerprint": "t1", "head_sha": "", "dirty": False}, CurrentCode("", "h2"), "not_checked"),
        ({"tree_fingerprint": "t1", "head_sha": "h1", "dirty": False}, CurrentCode("", ""), "not_checked"),
    ],
)
def test_code_check_compares_the_tree_first_and_a_head_only_when_it_names_the_content(
    snapshot: dict[str, Any], current: CurrentCode | None, expected: str
) -> None:
    assert code_check(snapshot, current) == expected


def test_each_outcome_has_its_one_plain_line(tmp_path: Path) -> None:
    changed = CurrentCode(tree_fingerprint="tree-other", head_sha="8c3d0a1" + "0" * 33)
    retraction = RetractionFact(by="srivera", at="2026-11-07T15:10:00+00:00")
    assert _outcome(_record(tmp_path)).headline() == f"Valid · signed by key {KEY_ID} at 09:00 UTC · matches ccccccc"
    assert _outcome(_record(tmp_path), current=None).headline().endswith(" · code not checked")
    own = _outcome(_record(tmp_path), own_key_id=KEY_ID).headline()
    assert own == "Valid · signed by you at 09:00 UTC · matches ccccccc"
    assert (
        _outcome(_record(tmp_path), current=changed).headline()
        == "Not valid · the code changed after signing (now 8c3d0a1)"
    )
    assert (
        _outcome(_record(tmp_path), keys={}).headline()
        == f"Can't verify · key {KEY_ID} is not in your trusted signing keys"
    )
    revoked = _keys(status="revoked", revoked_at="2026-11-06T00:00:00+00:00")
    assert _outcome(_record(tmp_path), keys=revoked).headline() == (
        f"Can't verify · key {KEY_ID} was revoked before this record was signed"
    )
    assert _outcome(_record(tmp_path), retraction=retraction).headline() == "Retracted · by @srivera at 15:10 UTC"
    assert _outcome(_record(tmp_path, signed=False)).headline() == "Can't verify · this record is not signed"
    envelope = _record(tmp_path)
    envelope["signatures"][0]["sig"] = base64.b64encode(b"x" * 64).decode("ascii")
    assert _outcome(envelope).headline() == "Not valid · the record was altered after signing"


def test_signed_by_names_the_key_that_signed_and_you_only_for_the_viewers_own_key(tmp_path: Path) -> None:
    """A record decided by one person but signed by a key is named by the key; "you" needs the viewer's own key."""

    envelope = _record(tmp_path)
    assert _outcome(envelope).headline().startswith(f"Valid · signed by key {KEY_ID} at ")
    assert _outcome(envelope, own_key_id=KEY_ID).headline().startswith("Valid · signed by you at ")
    assert (
        _outcome(envelope, own_key_id="user-someone-else").headline().startswith(f"Valid · signed by key {KEY_ID} at ")
    )
    assert _outcome(envelope, own_key_id="").headline().startswith(f"Valid · signed by key {KEY_ID} at ")
    # The wire says which it was, so a surface never guesses.
    assert _outcome(envelope, own_key_id=KEY_ID).to_wire()["signed_by_you"] is True
    assert _outcome(envelope).to_wire()["signed_by_you"] is False


def test_a_working_tree_record_that_matches_names_the_captured_revision_not_a_commit(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context = replace(context, revision=replace(context.revision, head_sha="", dirty=True))
    envelope = dict(
        sign_review_record(
            context, _decisions()["landing"], signer=_signer(), issued_at=ISSUED_AT, persist=False
        ).envelope
    )
    result = _outcome(envelope, current=CurrentCode(tree_fingerprint=SIGNED_TREE))
    assert (result.outcome, result.code) == ("valid", "matches")
    assert result.headline().endswith(" · matches the captured revision")


def test_a_dirty_snapshot_matched_by_tree_never_names_a_commit(tmp_path: Path) -> None:
    """The head of a dirty working tree does not name its content, even when the record carries one."""

    context = _context(tmp_path)
    context = replace(context, revision=replace(context.revision, head_sha="c" * 40, dirty=True))
    envelope = dict(
        sign_review_record(
            context, _decisions()["landing"], signer=_signer(), issued_at=ISSUED_AT, persist=False
        ).envelope
    )
    result = _outcome(envelope, current=CurrentCode(tree_fingerprint=SIGNED_TREE, head_sha="d" * 40))
    assert (result.outcome, result.code) == ("valid", "matches")
    assert result.headline().endswith(" · matches the captured revision") and "ccccccc" not in result.headline()


def test_a_forged_key_id_is_cant_verify_never_valid(tmp_path: Path) -> None:
    """An attacker signs with their own key under an id the trusted set does not list."""

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from lemoncrow.pro.capabilities.review.record_signing import Ed25519RecordSigner

    attacker = Ed25519RecordSigner(
        Ed25519PrivateKey.from_private_bytes(bytes([5]) * 32), key_id="user-forged", mode="user_key"
    )
    forged = dict(
        sign_review_record(
            _context(tmp_path), _decisions()["landing"], signer=attacker, issued_at=ISSUED_AT, persist=False
        ).envelope
    )
    result = _outcome(forged)
    assert (result.outcome, result.key_problem) == ("key_not_allowed", "not_trusted")
    assert result.headline() == "Can't verify · key user-forged is not in your trusted signing keys"
    # Claiming the trusted key's id while signing with another key is an altered record, not a trusted one.
    claimed = Ed25519RecordSigner(Ed25519PrivateKey.from_private_bytes(bytes([5]) * 32), key_id=KEY_ID, mode="user_key")
    impostor = dict(
        sign_review_record(
            _context(tmp_path), _decisions()["landing"], signer=claimed, issued_at=ISSUED_AT, persist=False
        ).envelope
    )
    assert _outcome(impostor).outcome == "not_valid"
    # Re-labelling a genuine signature with an id the payload does not name is refused too.
    relabelled = _record(tmp_path)
    relabelled["signatures"][0]["keyid"] = "user-forged"
    assert _outcome(relabelled).outcome == "not_valid"
