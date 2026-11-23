"""What a person is told about one signed record: Valid, Code changed, Key not allowed, Retracted.

:func:`record_outcome` wraps :func:`.record_verify.verify_record` (which keeps its
own verdicts and exit codes) and adds the two questions the Verify dialog asks
that a signature alone cannot answer: is the code still the code that was
signed, and has the record been retracted. Pure: no store, no clock besides *now*.

Precedence, first match wins: ``unsigned``; ``key_not_allowed`` (the key is not in
the trusted key set, or was revoked before the record was issued); ``not_valid``
(any other failed check, including an altered record); ``retracted``;
``code_changed``; ``valid``. A record that cannot be verified is never also
called retracted or changed, so an unverifiable file cannot be made to look
"fine but retracted".

"Code changed" compares the signed Snapshot with the latest *captured* revision
the caller supplies, not with a live checkout. With nothing to compare it says
``not_checked``, never ``matches``.

Exit codes (``lc review verify``): valid 0, not_valid 1, unsigned 2,
key_not_allowed 3, code_changed 4, retracted 5.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.freshness import parse_timestamp
from lemoncrow.pro.capabilities.review.record_retraction import verify_retraction
from lemoncrow.pro.capabilities.review.record_verify import (
    PublishedKey,
    RecordVerification,
    envelope_of,
    verify_record,
)

RecordOutcome = Literal["valid", "code_changed", "key_not_allowed", "retracted", "not_valid", "unsigned"]
CodeCheck = Literal["matches", "changed", "not_checked"]
KeyProblem = Literal["", "not_trusted", "revoked"]

OUTCOME_EXIT_CODES: Final[Mapping[str, int]] = {
    "valid": 0,
    "not_valid": 1,
    "unsigned": 2,
    "key_not_allowed": 3,
    "code_changed": 4,
    "retracted": 5,
}


@dataclass(frozen=True, slots=True)
class CurrentCode:
    """The code to compare a record with: the latest captured revision's identity."""

    tree_fingerprint: str = ""
    head_sha: str = ""


@dataclass(frozen=True, slots=True)
class RetractionFact:
    """A retraction of this record: who retracted it and when (P6-2: read from the signed retraction)."""

    by: str
    at: str


def _text(mapping: object, key: str) -> str:
    value = mapping.get(key) if isinstance(mapping, Mapping) else None
    return value if isinstance(value, str) else ""


def _clock(instant: str) -> str:
    """``14:02 UTC`` for an aware ISO instant ("" when unreadable)."""

    parsed = parse_timestamp(instant)
    return "" if parsed is None else parsed.astimezone(UTC).strftime("%H:%M UTC")


def code_check(snapshot: Mapping[str, Any], current: CurrentCode | None) -> CodeCheck:
    """Whether *current* is the code the record signed.

    The semantic tree fingerprint decides when both sides have one. A head commit
    decides only when the signed Snapshot was not dirty (a dirty working tree has a
    head that does not name its content). Anything else is ``not_checked``.
    """

    if current is None:
        return "not_checked"
    signed_tree = _text(snapshot, "tree_fingerprint")
    if signed_tree and current.tree_fingerprint:
        return "matches" if signed_tree == current.tree_fingerprint else "changed"
    signed_head = _text(snapshot, "head_sha")
    if signed_head and current.head_sha and snapshot.get("dirty") is False:
        return "matches" if signed_head == current.head_sha else "changed"
    return "not_checked"


@dataclass(frozen=True, slots=True)
class OutcomeResult:
    outcome: RecordOutcome
    verification: RecordVerification
    code: CodeCheck = "not_checked"
    key_problem: KeyProblem = ""
    signed_head_sha: str = ""
    current_head_sha: str = ""
    signed_by: str = ""
    signed_at: str = ""
    key_id: str = ""
    retraction: RetractionFact | None = None
    #: The key that signed is this viewer's own personal key (never inferred from who decided).
    signed_by_you: bool = False
    #: The signed Snapshot was a working tree, so its commit does not name its content.
    signed_dirty: bool = False
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def exit_code(self) -> int:
        return OUTCOME_EXIT_CODES[self.outcome]

    def headline(self) -> str:
        """The one plain line every surface shows (SPEC 4.18), with honest wording where SPEC's would be false."""

        signed_at = _clock(self.signed_at)
        # Named by the key that signed, not by who decided: a merge record is signed by a key too.
        who = "you" if self.signed_by_you else f"key {self.key_id}"
        matched = "" if self.signed_dirty else self.signed_head_sha[:7]
        if self.outcome == "valid":
            parts = [f"Valid · signed by {who}" + (f" at {signed_at}" if signed_at else "")]
            if self.code != "matches":
                parts.append("code not checked")
            else:
                # A working-tree review has no commit to name: say what it matched instead.
                parts.append(f"matches {matched}" if matched else "matches the captured revision")
            return " · ".join(parts)
        if self.outcome == "code_changed":
            now = self.current_head_sha[:7]
            return "Not valid · the code changed after signing" + (f" (now {now})" if now else "")
        if self.outcome == "key_not_allowed":
            if self.key_problem == "revoked":
                return f"Can't verify · key {self.key_id} was revoked before this record was signed"
            return f"Can't verify · key {self.key_id} is not in your trusted signing keys"
        if self.outcome == "retracted":
            by = self.retraction.by if self.retraction is not None else ""
            at = _clock(self.retraction.at) if self.retraction is not None else ""
            return "Retracted" + (f" · by @{by}" if by else "") + (f" at {at}" if at else "")
        if self.outcome == "unsigned":
            return "Can't verify · this record is not signed"
        altered = any("does not match the payload" in reason or "not canonical" in reason for reason in self.reasons)
        if altered or not self.reasons:
            return "Not valid · the record was altered after signing"
        return f"Not valid · {self.reasons[0]}"

    def to_wire(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "code": self.code,
            "key_problem": self.key_problem,
            "key_id": self.key_id,
            "signed_by": self.signed_by,
            "signed_by_you": self.signed_by_you,
            "signed_at": self.signed_at,
            "signed_head_sha": self.signed_head_sha,
            "current_head_sha": self.current_head_sha,
            "retraction": None if self.retraction is None else {"by": self.retraction.by, "at": self.retraction.at},
            "reasons": list(self.reasons),
            "headline": self.headline(),
        }


def record_outcome(
    document: object,
    *,
    keys: Mapping[str, PublishedKey] | None,
    current: CurrentCode | None,
    retraction: RetractionFact | None = None,
    retraction_document: object = None,
    own_key_id: str = "",
    now: datetime | None = None,
) -> OutcomeResult:
    """Verify *document* offline and say which of the Verify dialog's outcomes it is.

    *own_key_id* is the viewer's own personal key, when the caller knows it: only a record signed by that key
    reads "signed by you".

    *retraction_document* is the signed retraction envelope someone supplied for this record (P6-2). It is checked
    here, after the record itself: a valid one makes the outcome ``retracted``; one that does not withdraw this
    record (wrong key, wrong record, too late, altered) makes it ``not_valid`` with the reason, because a record
    that was said to be retracted must not read Valid. *retraction* is an already-checked fact (used by tests).
    """

    verification = verify_record(document, keys=keys, now=now)
    summary = verification.summary
    facts: dict[str, Any] = {
        "verification": verification,
        "key_id": verification.key_id,
        "signed_by": _text(summary, "decided_by"),
        "signed_at": _text(summary, "issued_at"),
        "signed_head_sha": _text(summary, "head_sha"),
        "current_head_sha": current.head_sha if current is not None else "",
        "reasons": verification.reasons,
    }
    facts["signed_by_you"] = (
        bool(own_key_id) and verification.key_id == own_key_id and verification.signing_mode == "user_key"
    )
    if verification.status == "unsigned":
        return OutcomeResult("unsigned", **facts)
    if verification.status == "revoked":
        return OutcomeResult("key_not_allowed", key_problem="revoked", **facts)
    if verification.status == "invalid":
        if verification.key_id and verification.key_id not in (keys or {}):
            return OutcomeResult("key_not_allowed", key_problem="not_trusted", **facts)
        return OutcomeResult("not_valid", **facts)
    if retraction_document is not None:
        check = verify_retraction(retraction_document, record=verification, keys=keys, now=now)
        if not check.valid:
            return OutcomeResult("not_valid", **{**facts, "reasons": check.reasons})
        retraction = RetractionFact(by=check.retracted_by, at=check.retracted_at)
    if retraction is not None:
        return OutcomeResult("retracted", retraction=retraction, **facts)
    snapshot = _signed_snapshot(document)
    checked = code_check(snapshot, current)
    if checked == "changed":
        return OutcomeResult("code_changed", code="changed", **facts)
    return OutcomeResult("valid", code=checked, signed_dirty=bool(snapshot.get("dirty")), **facts)


def _signed_snapshot(document: object) -> Mapping[str, Any]:
    """The Snapshot section of an already-verified record's payload."""

    envelope = envelope_of(document)
    if envelope is None:
        return {}
    statement = json.loads(base64.b64decode(str(envelope.get("payload") or "")).decode("utf-8"))
    predicate = statement.get("predicate") if isinstance(statement, Mapping) else None
    snapshot = predicate.get("snapshot") if isinstance(predicate, Mapping) else None
    return snapshot if isinstance(snapshot, Mapping) else {}


__all__ = [
    "OUTCOME_EXIT_CODES",
    "CodeCheck",
    "CurrentCode",
    "OutcomeResult",
    "RecordOutcome",
    "RetractionFact",
    "code_check",
    "record_outcome",
]
