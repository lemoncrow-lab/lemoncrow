"""Retracting a signed record (SPEC 4.18, P6-2): a statement signed by the same key, inside 24 hours of signing.

A signed record is evidence and never changes. A person who sent a verdict they no longer stand behind
retracts it with a second, separate DSSE envelope: an in-toto Statement whose predicate names the record it
withdraws (by its payload digest), who retracts it and when, signed by the key that signed the record. The
record's own file is untouched, so it still verifies; whoever verifies it is also given the retraction, and a
record with a valid retraction reads Retracted (:mod:`.record_outcome`).

The window is the record's own *Signed at* (``predicate.issued_at``) plus 24 hours, read on the one clock the
server signs with. The server refuses a retraction made after that; the offline verifier checks the same bound
again, so a retraction that claims a later time is not valid wherever it is read. A product build can only
lengthen nothing: the server's window may be tightened (tests), never widened past 24 hours.

Known limit, as for records: ``retracted_at`` is asserted by the signer, so a key holder could back-date one.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.freshness import parse_timestamp
from lemoncrow.pro.capabilities.review.record_signing import (
    RecordSigner,
    SignedReviewRecord,
    dsse_envelope,
    list_records,
    pae,
)
from lemoncrow.pro.capabilities.review.record_verify import (
    PublishedKey,
    RecordVerification,
    _signature_valid,
    envelope_of,
)
from lemoncrow.pro.capabilities.review.review_record import (
    PAYLOAD_TYPE,
    STATEMENT_TYPE,
    canonical_json,
)

RETRACTION_PREDICATE_TYPE: Final[str] = "https://lemoncrow.dev/attestations/review-retraction/v1"
RETRACTION_SCHEMA_VERSION: Final[int] = 1
#: The relationship kind under which retractions are persisted on their Change (one row per retracted record).
RETRACTION_RELATIONSHIP_KIND: Final[str] = "review_record_retraction"
#: SPEC 4.18: signed time + 24 h. The ceiling the verifier enforces; a server may only tighten it.
RETRACT_WINDOW: Final[timedelta] = timedelta(hours=24)
_FUTURE_SKEW: Final[timedelta] = timedelta(minutes=5)

#: The outcomes that are a person's verdict (a comment is not one).
_DECISION_OUTCOMES: Final[frozenset[str]] = frozenset({"lgtm", "changes_requested"})

RetractionRefusal = Literal[
    "not_a_verdict", "unsigned", "not_your_verdict", "window_closed", "different_key", "not_signed_here"
]


class RetractionRefused(Exception):
    """The server will not sign this retraction; nothing was written.

    ``code``: ``not_a_verdict`` (only a person's own verdict can be retracted), ``unsigned`` (an unsigned record
    has no key to retract with), ``not_your_verdict`` (someone else decided it), ``window_closed`` (24 hours from
    signing have passed) or ``different_key`` (the key that signed the record is not the key signing now).
    """

    def __init__(self, code: RetractionRefusal, reason: str, *, retract_until: str = "") -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.retract_until = retract_until


@dataclass(frozen=True, slots=True)
class SignedRetraction:
    """One stored retraction: the envelope plus the facts a list view needs."""

    record_id: str
    review_id: str
    record_digest: str
    retracted_at: str
    retracted_by: str
    key_id: str
    envelope: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RetractState:
    """Where one record stands with its retraction window (what Details and the record line show)."""

    state: Literal["open", "closed", "retracted"]
    #: The end of the window as an aware ISO instant ("" for a record with no readable signing time).
    until: str
    retracted_at: str = ""
    retracted_by: str = ""
    #: The window is the full 24 hours of SPEC (false only when a test tightened it).
    full_window: bool = True


def retract_until(issued_at: str, window: timedelta = RETRACT_WINDOW) -> datetime | None:
    """The first instant at which a retraction is too late: signed time + *window*, never more than 24 hours."""

    signed = parse_timestamp(issued_at)
    return None if signed is None else signed + min(window, RETRACT_WINDOW)


def window_open(issued_at: str, now: str, window: timedelta = RETRACT_WINDOW) -> bool:
    """True while *now* is before signed time + window. Exactly at the end the window is closed."""

    until, moment = retract_until(issued_at, window), parse_timestamp(now)
    return until is not None and moment is not None and moment < until


def retract_state(
    record: SignedReviewRecord, retraction: SignedRetraction | None, now: str, window: timedelta = RETRACT_WINDOW
) -> RetractState:
    until = retract_until(record.issued_at, window)
    shown = "" if until is None else until.astimezone(UTC).isoformat()
    full = min(window, RETRACT_WINDOW) == RETRACT_WINDOW
    if retraction is not None:
        return RetractState("retracted", shown, retraction.retracted_at, retraction.retracted_by, full)
    return RetractState("open" if window_open(record.issued_at, now, window) else "closed", shown, full_window=full)


def build_retraction_statement(
    record: SignedReviewRecord,
    *,
    retracted_by: str,
    retracted_by_type: str,
    retracted_at: str,
    key_id: str,
    algorithm: str,
    mode: str,
    window: timedelta = RETRACT_WINDOW,
) -> dict[str, Any]:
    """The in-toto Statement withdrawing *record* (pure)."""

    until = retract_until(record.issued_at, window)
    if until is None or parse_timestamp(retracted_at) is None:
        raise ValueError("a retraction names an aware signing time and an aware retraction time")
    return {
        "_type": STATEMENT_TYPE,
        "subject": [
            {
                "name": f"lemoncrow-review/{record.review_id}/record/{record.record_id}",
                "digest": {"sha256": record.payload_digest.removeprefix("sha256:")},
            }
        ],
        "predicateType": RETRACTION_PREDICATE_TYPE,
        "predicate": {
            "schema_version": RETRACTION_SCHEMA_VERSION,
            "record": {
                "record_id": record.record_id,
                "review_id": record.review_id,
                "revision_id": record.revision_id,
                "payload_digest": record.payload_digest,
                "issued_at": record.issued_at,
                "key_id": record.key_id,
            },
            "retracted_by": {"id": retracted_by, "type": retracted_by_type or "unknown"},
            "retracted_at": retracted_at,
            "retract_until": until.astimezone(UTC).isoformat(),
            "signing": {"state": "signed", "mode": mode, "key_id": key_id, "algorithm": algorithm},
        },
    }


def sign_retraction(
    record: SignedReviewRecord,
    signer: RecordSigner,
    *,
    retracted_by: str,
    retracted_by_type: str,
    retracted_at: str,
    window: timedelta = RETRACT_WINDOW,
) -> SignedRetraction:
    """Check the rules, then sign. Raises :class:`RetractionRefused`; nothing is persisted here.

    Order matters for the reason a person is told: a record that can never be retracted (not a verdict, unsigned,
    someone else's) is named before the clock, and the clock before the key.
    """

    if record.decision_kind != "human_outcome":
        raise RetractionRefused("not_a_verdict", "only a verdict you sent can be retracted")
    if not record.signed:
        raise RetractionRefused("unsigned", "an unsigned record has no key to retract it with")
    until = retract_until(record.issued_at, window)
    shown = "" if until is None else until.astimezone(UTC).isoformat()
    if not window_open(record.issued_at, retracted_at, window):
        raise RetractionRefused(
            "window_closed",
            "the 24 hours from signing have passed; a record can no longer be retracted",
            retract_until=shown,
        )
    identity = signer.identity
    if identity.key_id != record.key_id:
        raise RetractionRefused(
            "different_key", "the key that signed this record is not the key signing now", retract_until=shown
        )
    statement = build_retraction_statement(
        record,
        retracted_by=retracted_by,
        retracted_by_type=retracted_by_type,
        retracted_at=retracted_at,
        key_id=identity.key_id,
        algorithm=identity.algorithm,
        mode=identity.mode,
        window=window,
    )
    payload = canonical_json(statement)
    return SignedRetraction(
        record_id=record.record_id,
        review_id=record.review_id,
        record_digest=record.payload_digest,
        retracted_at=retracted_at,
        retracted_by=retracted_by,
        key_id=identity.key_id,
        envelope=dsse_envelope(payload, signer),
    )


# --------------------------------------------------------------------------- #
# persistence (an existing relationship row per retraction; no new table)
# --------------------------------------------------------------------------- #


def persist_retraction(store: Any, retraction: SignedRetraction) -> None:
    """Store the retraction on its Change, keyed by the record's payload digest.

    Written by the retract route and removed only with its Change (the relationship rows go when the Change
    does). It is never edited or deleted on its own: a retraction cannot be taken back, a new send signs a new
    record. The writer checks for an existing row first (``retract_record``), because a second write on the
    same key would replace the first.
    """

    from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship

    store.upsert_relationship(
        ReviewRelationship(
            id="",
            review_id=retraction.review_id,
            kind=RETRACTION_RELATIONSHIP_KIND,
            relation="dsse_envelope",
            provider="user_key",
            external_id=retraction.record_digest,
            title=f"Retraction of {retraction.record_id}",
            status="retracted",
            source="lemoncrow",
            metadata_json=json.dumps(
                {
                    "record_id": retraction.record_id,
                    "record_digest": retraction.record_digest,
                    "retracted_at": retraction.retracted_at,
                    "retracted_by": retraction.retracted_by,
                    "key_id": retraction.key_id,
                    "envelope": dict(retraction.envelope),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    )


def _from_row(row: Any) -> SignedRetraction | None:
    try:
        metadata = json.loads(str(getattr(row, "metadata_json", "") or "{}"))
    except ValueError:
        return None
    envelope = metadata.get("envelope") if isinstance(metadata, Mapping) else None
    if not isinstance(envelope, Mapping):
        return None
    return SignedRetraction(
        record_id=str(metadata.get("record_id") or ""),
        review_id=str(getattr(row, "review_id", "") or ""),
        record_digest=str(metadata.get("record_digest") or ""),
        retracted_at=str(metadata.get("retracted_at") or ""),
        retracted_by=str(metadata.get("retracted_by") or ""),
        key_id=str(metadata.get("key_id") or ""),
        envelope=dict(envelope),
    )


def list_retractions(store: Any, review_id: str) -> dict[str, SignedRetraction]:
    """The retractions of one Change by the id of the record each withdraws."""

    rows = store.list_relationships(review_id, kind=RETRACTION_RELATIONSHIP_KIND)
    found = (_from_row(row) for row in rows)
    return {item.record_id: item for item in found if item is not None and item.record_id}


def get_retraction(store: Any, review_id: str, record_id: str) -> SignedRetraction | None:
    return list_retractions(store, review_id).get(record_id)


def retracted_outcome_ids(store: Any, review_id: str) -> frozenset[str]:
    """Ids of the verdict rows a person retracted and has not sent again.

    A verdict stands again as soon as its newest signed record is not retracted (a resend signs a new record for the
    same stored verdict row), so this is "the newest human record of that verdict is retracted". A verdict with no
    record (never signed) is never in it. With no retraction on the Change it costs one small query.
    """

    retractions = list_retractions(store, review_id)
    if not retractions:
        return frozenset()
    newest: dict[str, str] = {}
    for record in list_records(store, review_id):  # newest first
        if record.decision_kind != "human_outcome":
            continue
        decision = record.statement.get("predicate", {}).get("decision", {})
        reference = str(decision.get("reference") or "") if isinstance(decision, Mapping) else ""
        if reference:
            newest.setdefault(reference, record.record_id)
    return frozenset(reference for reference, record_id in newest.items() if record_id in retractions)


def withheld_outcome_ids(store: Any, review_id: str, *, revision_id: str = "") -> frozenset[str]:
    """Verdict rows that stand for nobody: every retracted verdict, and every older verdict of a reviewer whose
    newest verdict is retracted (the person has no standing verdict; an older one does not take its place).

    *revision_id* limits the walk to the verdicts on that revision (the decision a record names is per revision);
    without it the walk is over all of the Change's verdicts (what readiness reads). The one rule behind both
    :func:`standing_outcomes` and the decision a merge or an export names, so they cannot drift.
    """

    retracted = retracted_outcome_ids(store, review_id)
    if not retracted:
        return frozenset()
    withheld: set[str] = set()
    decided: set[str] = set()
    silenced: set[str] = set()
    for outcome in store.list_outcomes(review_id):  # newest first
        if revision_id and outcome.revision_id != revision_id:
            continue
        if outcome.outcome not in _DECISION_OUTCOMES:
            continue  # a comment is no verdict: it neither decides who is newest nor stands in for one
        newest = outcome.reviewer_id not in decided
        decided.add(outcome.reviewer_id)
        if outcome.id in retracted:
            withheld.add(outcome.id)
            if newest:
                silenced.add(outcome.reviewer_id)
        elif outcome.reviewer_id in silenced:
            withheld.add(outcome.id)
    return frozenset(withheld)


def standing_outcomes(store: Any, review_id: str) -> tuple[Any, ...]:
    """Each reviewer's newest verdict, leaving out one that was retracted (SPEC 4.18).

    The one place readiness, auto-approval facts and the related-change state read a verdict from, so a retracted
    approval (or retracted request for changes) counts for nothing until the person sends again. A retracted newest
    verdict is not replaced by an older one: the person has no standing verdict.
    """

    withheld = withheld_outcome_ids(store, review_id)
    return tuple(item for item in store.latest_outcomes(review_id) if item.id not in withheld)


# --------------------------------------------------------------------------- #
# offline verification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RetractionCheck:
    """Whether a retraction document validly withdraws one verified record."""

    valid: bool
    reasons: tuple[str, ...] = ()
    retracted_by: str = ""
    retracted_at: str = ""


def _map(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def verify_retraction(
    document: object,
    *,
    record: RecordVerification,
    keys: Mapping[str, PublishedKey] | None,
    now: datetime | None = None,
) -> RetractionCheck:
    """Check *document* offline against the already-verified *record*. Every rule must hold; there is no partial credit.

    The retraction is valid only when its signature verifies with a trusted key, that key is the key that signed
    the record, it names this record's payload digest, it was made by the person who decided, and it was made
    inside the window: not before the record was signed, and before signed time + 24 hours (and before the
    earlier end the statement itself claims). The key must not have been revoked, or retired, at that time.
    """

    def refuse(reason: str) -> RetractionCheck:
        return RetractionCheck(False, (f"the retraction {reason}",))

    envelope = envelope_of(document)
    if envelope is None:
        return refuse("is not a DSSE envelope")
    if envelope.get("payloadType") != PAYLOAD_TYPE:
        return refuse(f"has a payloadType that is not {PAYLOAD_TYPE}")
    try:
        payload = base64.b64decode(str(envelope.get("payload") or ""), validate=True)
        statement = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return refuse("has a payload that is not base64 JSON")
    if not isinstance(statement, dict):
        return refuse("has a payload that is not a JSON object")
    try:
        if canonical_json(statement) != payload:
            return refuse("has a payload that is not in canonical form")
    except ValueError:
        return refuse("has a payload that is not in canonical form")
    predicate = _map(statement.get("predicate"))
    if statement.get("_type") != STATEMENT_TYPE or statement.get("predicateType") != RETRACTION_PREDICATE_TYPE:
        return refuse("is not a review-record retraction")
    if predicate.get("schema_version") != RETRACTION_SCHEMA_VERSION:
        return refuse("has an unsupported schema version")
    named = _map(predicate.get("record"))
    signing = _map(predicate.get("signing"))
    by = _text(_map(predicate.get("retracted_by")).get("id"))
    retracted_at = parse_timestamp(_text(predicate.get("retracted_at")))
    claimed_until = parse_timestamp(_text(predicate.get("retract_until")))
    if not by or retracted_at is None or claimed_until is None:
        return refuse("does not name who retracted, when, and until when")
    signatures = envelope.get("signatures")
    if not isinstance(signatures, list) or len(signatures) != 1 or not isinstance(signatures[0], Mapping):
        return refuse("carries no single signature")
    kid = _text(signatures[0].get("keyid"))
    if kid != record.key_id or _text(signing.get("key_id")) != record.key_id:
        return refuse("was signed by a different key than the record")
    key = (keys or {}).get(kid)
    if key is None:
        return refuse(f"was signed by key {kid}, which is not in your trusted signing keys")
    if _text(signing.get("algorithm")) != key.algorithm:
        return refuse(f"claims a different algorithm than key {kid}")
    try:
        signature = base64.b64decode(_text(signatures[0].get("sig")), validate=True)
    except (ValueError, binascii.Error):
        return refuse("has a signature that is not base64")
    if not _signature_valid(key, signature, pae(PAYLOAD_TYPE, payload)):
        return refuse("has a signature that is not valid")
    if _text(named.get("payload_digest")) != record.payload_digest:
        return refuse("is for a different record")
    signed_by = _text(record.summary.get("decided_by"))
    if by != signed_by:
        return refuse(f"was made by {by}, not by {signed_by}, who decided")
    signed_at = parse_timestamp(_text(record.summary.get("issued_at")))
    if signed_at is None:
        return refuse("cannot be placed in time: the record has no readable signing time")
    if retracted_at < signed_at:
        return refuse("is dated before the record was signed")
    if retracted_at >= signed_at + RETRACT_WINDOW or retracted_at >= claimed_until:
        return refuse("was made after the 24 hours from signing")
    if retracted_at > (now or datetime.now(UTC)) + _FUTURE_SKEW:
        return refuse("claims to be made in the future")
    if key.revoked_at is not None and retracted_at >= key.revoked_at:
        return refuse(f"was made after key {kid} was revoked")
    if key.not_before is not None and retracted_at < key.not_before:
        return refuse(f"was made before key {kid} was valid")
    if key.not_after is not None and retracted_at >= key.not_after:
        return refuse(f"was made after key {kid} had stopped signing")
    return RetractionCheck(True, (), by, retracted_at.astimezone(UTC).isoformat())


__all__ = [
    "RETRACTION_PREDICATE_TYPE",
    "RETRACTION_RELATIONSHIP_KIND",
    "RETRACTION_SCHEMA_VERSION",
    "RETRACT_WINDOW",
    "RetractState",
    "RetractionCheck",
    "RetractionRefused",
    "SignedRetraction",
    "build_retraction_statement",
    "get_retraction",
    "list_retractions",
    "persist_retraction",
    "retract_state",
    "retract_until",
    "retracted_outcome_ids",
    "sign_retraction",
    "standing_outcomes",
    "verify_retraction",
    "window_open",
    "withheld_outcome_ids",
]
