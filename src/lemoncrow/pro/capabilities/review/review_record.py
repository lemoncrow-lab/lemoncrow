"""Signed review record: the attested content (Phase 10 item 19).

A review record is an in-toto Statement v1 whose predicate binds one decided
Review Snapshot to everything the decision rested on: Snapshot identity
(review, revision, head/base SHAs, tree and source digests), evidence and check
results with their executor and attestation classes, human judgments and the
outcome, waivers, the readiness state and policy generation it was evaluated
under, the run plan digest, and the time and actor of the decision.

This module is pure content: no key material, no signing. The payload is
canonical JSON (sorted keys, no insignificant whitespace, UTF-8, no floats), so
its digest is reproducible from stored state. Rows are read *as of* the record
instant (``created_at <= as_of``) from append-only tables, so a later outcome
or evidence row never makes an earlier record disagree with the store; a
section that does disagree names exactly what changed.

Signing lives in :mod:`.record_signing`, verification in :mod:`.record_verify`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.freshness import parse_timestamp
from lemoncrow.pro.capabilities.review.readiness import conditions_digest
from lemoncrow.pro.capabilities.review.session_models import (
    Annotation,
    AnnotationVersion,
    ReviewActivityEvent,
    ReviewDecisionEvent,
    ReviewEvidence,
    ReviewMarkEvent,
    ReviewOutcome,
    ReviewRevision,
    ReviewSession,
)

STATEMENT_TYPE: Final[str] = "https://in-toto.io/Statement/v1"
#: Versioned predicate type. A breaking predicate change is a new ``/vN`` URI.
PREDICATE_TYPE: Final[str] = "https://lemoncrow.dev/attestations/review-record/v1"
PREDICATE_TYPE_V2: Final[str] = "https://lemoncrow.dev/attestations/review-record/v2"
#: DSSE ``payloadType`` of an in-toto Statement.
PAYLOAD_TYPE: Final[str] = "application/vnd.in-toto+json"
RECORD_SCHEMA_VERSION: Final[int] = 1
RECORD_SCHEMA_VERSION_V2: Final[int] = 2

SigningMode = Literal["organization_key", "user_key", "sigstore", "unsigned"]
SIGNING_MODES: Final[tuple[str, ...]] = ("organization_key", "user_key", "sigstore", "unsigned")
DecisionKind = Literal["human_outcome", "landing", "provider_merge"]
DECISION_KINDS: Final[tuple[str, ...]] = ("human_outcome", "landing", "provider_merge")
#: Stated inside every unsigned payload so no consumer can mistake it for a signed one.
UNSIGNED_NOTICE: Final[str] = (
    "UNSIGNED: this record carries no signature and is not evidence of who produced it or that it is unaltered."
)
#: The relationship kind under which records are persisted on their Change.
RECORD_RELATIONSHIP_KIND: Final[str] = "review_record"

_DECISION_OUTCOMES: Final[Mapping[str, str]] = {"lgtm": "approved", "changes_requested": "changes_requested"}


class RecordSchemaError(ValueError):
    """A statement that does not satisfy the review-record predicate schema."""


def canonical_json(value: object) -> bytes:
    """The one byte encoding of a record payload (floats and NaN are refused)."""

    _refuse_floats(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _refuse_floats(value: object) -> None:
    if isinstance(value, float):
        raise RecordSchemaError("review record payloads carry no floating-point numbers")
    if isinstance(value, Mapping):
        for item in value.values():
            _refuse_floats(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _refuse_floats(item)


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest_of(value: object) -> str:
    return sha256_digest(canonical_json(value))


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """The decision a record is emitted after: who decided what, when.

    ``kind``: ``human_outcome`` (a reviewer's LGTM / changes requested on the
    Snapshot), ``landing`` (P10-LAND's landing decision, supplied through
    :func:`.record_signing.sign_review_record`) or ``provider_merge`` (the SCM
    merged the reviewed head). ``reference`` names the stored row the decision
    is (an outcome id, a landing relationship id).
    """

    kind: str
    outcome: str
    actor_id: str
    actor_type: str
    decided_at: str
    reference: str = ""
    detail: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in DECISION_KINDS:
            raise RecordSchemaError(f"unknown decision kind {self.kind!r}")
        if not self.outcome or not self.actor_id or parse_timestamp(self.decided_at) is None:
            raise RecordSchemaError("a decision names its outcome, actor and an aware decision time")

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "outcome": self.outcome,
            "actor": {"id": self.actor_id, "type": self.actor_type or "unknown"},
            "decided_at": self.decided_at,
            "reference": self.reference,
            "detail": {str(key): str(value) for key, value in sorted(self.detail.items())},
        }

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> ReviewDecision:
        actor = _mapping(wire.get("actor"))
        return cls(
            kind=str(wire.get("kind") or ""),
            outcome=str(wire.get("outcome") or ""),
            actor_id=str(actor.get("id") or ""),
            actor_type=str(actor.get("type") or ""),
            decided_at=str(wire.get("decided_at") or ""),
            reference=str(wire.get("reference") or ""),
            detail={str(key): str(value) for key, value in _mapping(wire.get("detail")).items()},
        )


@dataclass(frozen=True, slots=True)
class SigningIdentity:
    """Who signs (or that nobody does), stated inside the payload itself.

    A payload that says ``unsigned`` but carries a signature, or says signed but
    carries none, fails verification: stripping a signature can never turn a
    signed record into an "unsigned but otherwise fine" one, and an unsigned
    record can never be presented as signed.
    """

    mode: str
    key_id: str = ""
    algorithm: str = ""
    issuer_kind: str = ""
    issuer_id: str = ""

    def __post_init__(self) -> None:
        if self.mode not in SIGNING_MODES:
            raise RecordSchemaError(f"unknown signing mode {self.mode!r}")
        if self.mode != "unsigned" and (not self.key_id or not self.algorithm):
            raise RecordSchemaError("a signing identity names its key id and algorithm")
        if self.mode == "unsigned" and (self.key_id or self.algorithm):
            raise RecordSchemaError("an unsigned record names no key")

    @property
    def signed(self) -> bool:
        return self.mode != "unsigned"

    def to_wire(self) -> dict[str, Any]:
        if not self.signed:
            return {"state": "unsigned", "mode": "unsigned", "key_id": "", "algorithm": "", "notice": UNSIGNED_NOTICE}
        return {"state": "signed", "mode": self.mode, "key_id": self.key_id, "algorithm": self.algorithm, "notice": ""}

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> SigningIdentity:
        return cls(
            mode=str(wire.get("mode") or ""),
            key_id=str(wire.get("key_id") or ""),
            algorithm=str(wire.get("algorithm") or ""),
        )


UNSIGNED: Final[SigningIdentity] = SigningIdentity(mode="unsigned")


@dataclass(frozen=True, slots=True)
class RecordFacts:
    """Stored rows a record binds, read as of one instant."""

    evidence: tuple[ReviewEvidence, ...]
    waivers: tuple[ReviewEvidence, ...]
    outcomes: tuple[ReviewOutcome, ...]
    mark_events: tuple[ReviewMarkEvent, ...]
    decision_events: tuple[ReviewDecisionEvent, ...]
    annotations: tuple[Annotation, ...] = ()
    annotation_versions: tuple[tuple[str, tuple[AnnotationVersion, ...]], ...] = ()
    annotation_origins: tuple[tuple[str, str, str], ...] = ()
    coverage_events: tuple[ReviewActivityEvent, ...] = ()
    #: AI review hints raised on the revision, created at or before the record instant.
    ai_annotations: tuple[Annotation, ...] = ()


def _at_or_before(created_at: str, as_of: datetime) -> bool:
    instant = parse_timestamp(created_at)
    # An unreadable row timestamp is never silently included or dropped: it
    # counts as "before" so the record still binds it and a reader sees it.
    return instant is None or instant <= as_of


def gather_record_facts(
    store: Any, session: ReviewSession, revision: ReviewRevision, *, as_of: str, coverage_actor_id: str = ""
) -> RecordFacts:
    """Read the rows a record binds from the store, as of *as_of* (append-only tables).

    *coverage_actor_id* is whose self-reported coverage the record binds: the
    record's deciding reviewer, never whoever happens to be viewing (hosted
    verification runs as the viewer). Empty falls back to the session's reviewer.
    """

    instant = parse_timestamp(as_of)
    if instant is None:
        raise RecordSchemaError("as_of must be an aware ISO-8601 instant")
    every_evidence = tuple(item for item in store.list_evidence(session.id) if _at_or_before(item.created_at, instant))
    evidence = tuple(item for item in every_evidence if item.revision_id == revision.id)
    # A review-scoped waiver can be granted on an earlier Snapshot and still
    # apply; every waiver on the Change is bound, each with its own Snapshot.
    waivers = tuple(item for item in every_evidence if item.waived_requirement_key)
    outcomes = tuple(item for item in store.list_outcomes(session.id) if _at_or_before(item.created_at, instant))
    mark_events = tuple(
        item for item in store.list_review_mark_events(session.id) if _at_or_before(item.created_at, instant)
    )
    decision_events = tuple(
        item
        for item in store.list_decision_events(session.id, reviewer_id=None)
        if _at_or_before(item.created_at, instant)
    )
    every_note = tuple(store.list_annotations(session.id))
    annotations = tuple(
        note for note in every_note if note.reconsidered_after_structure and _at_or_before(note.created_at, instant)
    )
    ai_annotations = tuple(
        note
        for note in every_note
        if note.source == "ai_review"
        and not note.parent_id
        and note.revision_id == revision.id
        and _at_or_before(note.created_at, instant)
    )
    versions_by_id = store.annotation_versions_for_review(session.id) if annotations else {}
    from lemoncrow.pro.capabilities.review.seen_coverage import reviewer_coverage_events

    coverage_events = (
        reviewer_coverage_events(store, session, revision, as_of=as_of, reviewer_id=coverage_actor_id)
        if hasattr(store, "list_activity_events")
        else ()
    )
    return RecordFacts(
        evidence=evidence,
        waivers=waivers,
        coverage_events=coverage_events,
        ai_annotations=ai_annotations,
        outcomes=outcomes,
        mark_events=mark_events,
        decision_events=decision_events,
        annotations=annotations,
        annotation_versions=tuple(
            (
                note.id,
                tuple(
                    version for version in versions_by_id.get(note.id, ()) if _at_or_before(version.created_at, instant)
                ),
            )
            for note in annotations
        ),
        annotation_origins=tuple(
            (note.id, origin.path, origin.unit_key)
            for note in annotations
            if (origin := store.annotation_origin_anchor(note.id)) is not None
        ),
    )


def human_decision_for(outcome: ReviewOutcome) -> ReviewDecision | None:
    """The decision one stored outcome row is, or ``None`` for a comment (not a decision).

    Pinned to the row: signing it later never depends on what the Review's newest decision is by then.
    """

    kind = _DECISION_OUTCOMES.get(outcome.outcome)
    if kind is None:
        return None
    return ReviewDecision(
        kind="human_outcome",
        outcome=kind,
        actor_id=outcome.reviewer_id,
        actor_type="human",
        decided_at=outcome.created_at,
        reference=outcome.id,
    )


def current_decision(
    store: Any, session: ReviewSession, revision: ReviewRevision, *, include_retracted: bool = False
) -> ReviewDecision | None:
    """The decision on *revision* a record may be emitted after, or ``None``.

    A verdict the person retracted and has not sent again is no decision (SPEC 4.18), so a merge or an export never
    names it; the next decision (an auto-approval, a break-glass) is used instead. *include_retracted* keeps it, for
    the one caller that asks "is a signature for this verdict being made", which a resend of it is.

    A provider-observed merge of this Snapshot wins (it is the landing that
    happened); otherwise the newest human LGTM / changes-requested outcome on
    this exact Snapshot; otherwise the policy auto-approval in force on it, or
    the break-glass its landing rests on (P10-LAND). A ``comment`` outcome is
    not a decision.
    """

    from lemoncrow.pro.capabilities.review.auto_approval import (
        AUTO_APPROVAL_ACTOR,
        IN_FORCE_STATES,
        auto_approval_relationship,
        decision_for_record,
    )
    from lemoncrow.pro.capabilities.review.landing import landing_metadata, landing_relationship

    approval = auto_approval_relationship(store, session.id)
    approved_here = (
        approval is not None
        and approval.status in IN_FORCE_STATES
        and str(landing_metadata(approval).get("revision_id") or "") == revision.id
    )
    landing = landing_relationship(store, session.id)
    if landing is not None and landing.status == "merged":
        metadata = landing_metadata(landing)
        merged_revision = str(metadata.get("revision_id") or "")
        if merged_revision == revision.id:
            detail = {"mechanism": str(metadata.get("mechanism") or "")}
            # P10-LAND: what the landing rested on is part of the merge record.
            if metadata.get("basis"):
                detail["basis"] = str(metadata.get("basis"))
            if metadata.get("break_glass_id"):
                detail["break_glass_id"] = str(metadata.get("break_glass_id"))
                detail["bypassed"] = ",".join(str(item) for item in metadata.get("bypassed") or ())
            if approved_here:
                detail["auto_approval"] = str(landing_metadata(approval).get("decision_digest") or "")
            return ReviewDecision(
                kind="provider_merge",
                outcome="merged",
                actor_id=landing.provider or "scm",
                actor_type="system",
                decided_at=landing.updated_at,
                reference=landing.id,
                detail=detail,
            )
    from lemoncrow.pro.capabilities.review.record_retraction import withheld_outcome_ids

    # The same rule readiness reads (record_retraction.standing_outcomes): a retracted verdict, and the older verdicts
    # of the reviewer who retracted it, name no decision.
    retracted = frozenset() if include_retracted else withheld_outcome_ids(store, session.id, revision_id=revision.id)
    for outcome in store.list_outcomes(session.id):  # newest first
        if outcome.revision_id != revision.id or outcome.id in retracted:
            continue
        if (human := human_decision_for(outcome)) is not None:
            return human
    if approved_here and approval is not None:
        # No human decision: the policy auto-approval in force on this Snapshot is the decision.
        return ReviewDecision(
            kind="landing",
            outcome="auto_approved",
            actor_id=AUTO_APPROVAL_ACTOR,
            actor_type="system",
            decided_at=str(landing_metadata(approval).get("decided_at") or approval.updated_at),
            reference=approval.id,
            detail=decision_for_record(approval),
        )
    if landing is not None and landing.status != "stale":
        # No review decision: the break-glass this Snapshot's landing rests on is the decision.
        from lemoncrow.pro.capabilities.review import break_glass

        metadata = landing_metadata(landing)
        if metadata.get("basis") == "break_glass" and str(metadata.get("revision_id") or "") == revision.id:
            reference = str(metadata.get("break_glass_id") or "")
            record = next(
                (row for row in break_glass.records(store, [revision.id]) if row.get("id") == reference), None
            )
            if record is not None:
                return ReviewDecision(
                    kind="landing",
                    outcome="break_glass",
                    actor_id=str(record.get("actor_id") or ""),
                    actor_type="human",
                    decided_at=str(record.get("created_at") or ""),
                    reference=reference,
                    detail=break_glass.decision_detail(record),
                )
    return None


# --------------------------------------------------------------------------- #
# predicate sections
# --------------------------------------------------------------------------- #


def snapshot_section(session: ReviewSession, revision: ReviewRevision, *, number: int) -> dict[str, Any]:
    return {
        "review_id": session.id,
        "revision_id": revision.id,
        "number": int(number),
        "head_sha": revision.head_sha,
        "base_sha": revision.base_sha,
        "merge_base_sha": revision.merge_base_sha,
        "commit_sha": revision.commit_sha,
        "tree_fingerprint": revision.tree_fingerprint,
        "source_fingerprint": revision.source_fingerprint,
        "packet_sha256": revision.packet_sha256,
        "dirty": bool(revision.dirty),
    }


#: What the ``ai_hints`` section means, stated inside the payload (SPEC 4.18 "AI in this record").
AI_HINTS_BASIS: Final[str] = "raised_on_revision_not_proof_shown"
PART_STATES: Final[tuple[str, ...]] = ("reviewed", "needs_changes", "later")


@dataclass(frozen=True, slots=True)
class RecordPart:
    """One part of the change as the reviewer was asked about it, with the state Wrap up showed.

    ``category`` is the answer category (``decision``, ``quick_check``,
    ``accepted_by_rule``); ``state`` the part's mark state at the record instant.
    """

    unit_key: str
    label: str
    path: str
    category: str
    state: str
    rule: str = ""


def parts_section(parts: Sequence[RecordPart]) -> dict[str, Any]:
    """Parts by how they were asked and answered. Only stored facts: no count is invented.

    Accepted-by-rule parts are counted with their rule and listed nowhere else.
    A quick check carries no stored "quick-checked" method (P2-3 A), so the
    record lists the quick checks that were NOT confirmed and the ones marked
    Needs a change, and counts the rest only as quick checks.
    """

    counts: dict[str, int] = {"decision": 0, "quick_check": 0, "accepted_by_rule": 0}
    rules: dict[str, int] = {}
    decisions: list[dict[str, str]] = []
    quick_check_changes: list[dict[str, str]] = []
    not_confirmed: list[dict[str, str]] = []
    later = 0
    for part in sorted(parts, key=lambda row: (row.path, row.unit_key, row.label)):
        counts[part.category] = counts.get(part.category, 0) + 1
        if part.category == "accepted_by_rule":
            rules[part.rule] = rules.get(part.rule, 0) + 1
            continue
        row = {"unit_key": part.unit_key, "label": part.label, "path": part.path}
        if part.state == "later":
            later += 1
        if part.category == "decision":
            decisions.append({**row, "state": part.state if part.state in PART_STATES else "unanswered"})
        elif part.state == "needs_changes":
            quick_check_changes.append(row)
        elif part.state not in PART_STATES:
            not_confirmed.append(row)
    return {
        "counts": {**counts, "rules": dict(sorted(rules.items()))},
        "decisions": decisions,
        "quick_check_changes": quick_check_changes,
        "not_confirmed": not_confirmed,
        "later": later,
    }


def ai_hints_section(facts: RecordFacts, *, revision_id: str) -> dict[str, Any]:
    """AI hints raised on this revision. A hint is never a decision and its later fate is not bound."""

    return {
        "basis": AI_HINTS_BASIS,
        "hints": [
            {
                "id": note.id,
                "path": note.anchor.path,
                "side": note.anchor.side,
                "start_line": int(note.anchor.start_line),
                "end_line": int(note.anchor.end_line),
                "created_at": note.created_at,
            }
            for note in sorted(facts.ai_annotations, key=lambda row: (row.created_at, row.id))
            if note.revision_id == revision_id
        ],
    }


def change_section(revision: ReviewRevision) -> dict[str, str]:
    """Which coding-agent session produced this revision, as captured (certainty included)."""

    return {
        "agent_host": revision.provenance_host,
        "agent_session_id": revision.provenance_session_id,
        "provenance_certainty": str(revision.provenance_certainty),
    }


def _evidence_row(item: ReviewEvidence) -> dict[str, Any]:
    return {
        "id": item.id,
        "revision_id": item.revision_id,
        "kind": item.kind,
        "title": item.title,
        "stable_key": item.stable_key,
        "attestation_class": item.attestation_class,
        "executor_class": item.executor_class,
        "execution_intent": item.execution_intent,
        "state": item.verification_status,
        "source": item.source,
        "provider": item.provider,
        "provider_revision": item.provider_revision,
        "attested_by": item.attested_by,
        "observed_at": item.observed_at,
        "created_at": item.created_at,
        "content_hash": item.content_hash,
        "execution_verification_digest": (
            sha256_digest(item.execution_verification.encode("utf-8")) if item.execution_verification else ""
        ),
        "waived_requirement_key": item.waived_requirement_key,
    }


def evidence_section(facts: RecordFacts) -> list[dict[str, Any]]:
    return [_evidence_row(item) for item in sorted(facts.evidence, key=lambda row: row.id)]


def waivers_section(facts: RecordFacts) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in sorted(facts.waivers, key=lambda row: row.id):
        try:
            scope = json.loads(item.attestation_scope or "{}")
        except ValueError:
            scope = {}
        scope = scope if isinstance(scope, Mapping) else {}
        out.append(
            {
                "evidence_id": item.id,
                "revision_id": item.revision_id,
                "requirement_key": item.waived_requirement_key,
                "actor": item.attested_by or item.source_ref,
                "reason": item.detail,
                "scope": str(scope.get("scope") or ""),
                "expires_at": str(scope.get("expires_at") or ""),
                "policy_generation": str(scope.get("policy_generation") or ""),
                "created_at": item.created_at,
            }
        )
    return out


def judgments_section(facts: RecordFacts) -> dict[str, Any]:
    """Outcomes in full; per-target judgments and decisions as digest + counts.

    Per-target judgment events can number in the thousands on a large Change,
    so they are bound by a digest over their canonical rows (reproducible from
    the store) plus per-state counts rather than copied into every record.
    """

    outcomes = [
        {
            "id": item.id,
            "revision_id": item.revision_id,
            "reviewer_id": item.reviewer_id,
            "outcome": item.outcome,
            "created_at": item.created_at,
            "summary_digest": sha256_digest(item.summary.encode("utf-8")) if item.summary else "",
        }
        for item in sorted(facts.outcomes, key=lambda row: (row.created_at, row.id))
    ]
    marks = [
        {
            "id": item.id,
            "reviewer_id": item.reviewer_id,
            "unit_key": item.unit_key,
            "revision_id": item.revision_id,
            "event_kind": item.event_kind,
            "to_state": item.to_state,
            "content_fingerprint": item.content_fingerprint,
            "actor_type": item.actor_type,
            "created_at": item.created_at,
        }
        for item in sorted(facts.mark_events, key=lambda row: (row.created_at, row.id))
    ]
    by_state: dict[str, int] = {}
    for row in marks:
        state = str(row["to_state"] or "none")
        by_state[state] = by_state.get(state, 0) + 1
    decisions = [
        {
            "id": item.id,
            "decision_key": item.decision_key,
            "reviewer_id": item.reviewer_id,
            "revision_id": item.revision_id,
            "action": item.action,
            "disposition": item.disposition,
            "option_key": item.option_key,
            "semantic_fingerprint": item.semantic_fingerprint,
            "implementation_fingerprint": item.implementation_fingerprint,
            "actor_type": item.actor_type,
            "created_at": item.created_at,
        }
        for item in sorted(facts.decision_events, key=lambda row: (row.created_at, row.id))
    ]
    output: dict[str, Any] = {
        "outcomes": outcomes,
        "target_judgments": {"count": len(marks), "by_state": by_state, "digest": _digest_of(marks)},
        "decisions": {"count": len(decisions), "digest": _digest_of(decisions)},
    }
    # Optional additive v1 extension. It changes no historical statements: old
    # reviews without structured reconsideration serialize byte-for-byte as before.
    # Every future statement containing one binds the immutable creation context.
    versions_by_note = dict(facts.annotation_versions)
    origins_by_note = {note_id: (path, unit_key) for note_id, path, unit_key in facts.annotation_origins}
    structure = []
    for note in sorted(facts.annotations, key=lambda row: (row.created_at, row.id)):
        if not note.reconsidered_after_structure:
            continue
        versions = versions_by_note.get(note.id, ())
        version_rows = [
            {
                "id": version.id,
                "revision_id": version.revision_id,
                "number": version.version_number,
                "body_digest": sha256_digest(version.body.encode("utf-8")),
                "kind": version.kind,
                "state": version.state,
                "actor": version.changed_by,
                "change_kind": version.change_kind,
                "at": version.created_at,
            }
            for version in sorted(versions, key=lambda value: value.version_number)
        ]
        structure.append(
            {
                "id": note.id,
                "review_id": note.review_id,
                "revision_id": note.revision_id,
                "structure_revision_id": note.structure_revision_id,
                "original_path": origins_by_note.get(note.id, ("", ""))[0],
                "original_unit_key": origins_by_note.get(note.id, ("", ""))[1],
                "reviewer_id": note.created_by,
                "created_at": note.created_at,
                "source": note.source,
                "reported_after_structure": True,
                "annotation_history_digest": _digest_of(version_rows),
            }
        )
    if structure:
        output["structure_reconsiderations"] = {"count": len(structure), "digest": _digest_of(structure)}
    return output


def readiness_section(readiness: Mapping[str, Any]) -> dict[str, Any]:
    """The readiness evaluation the decision was recorded under (the readiness core's wire)."""

    landing = readiness.get("landing") if isinstance(readiness.get("landing"), Mapping) else {}
    conditions = [
        {
            "id": str(item.get("id") or ""),
            "label": str(item.get("label") or ""),
            "state": str(item.get("state") or ""),
            "detail": str(item.get("detail") or ""),
            "owner": str(item.get("owner") or ""),
            "category": str(item.get("category") or ""),
            "url": str(item.get("url") or ""),
            "reason": str(item.get("reason") or ""),
            "evidence_ids": sorted(
                str(ref.get("evidence_id") or "")
                for ref in item.get("evidence_refs", ()) or ()
                if isinstance(ref, Mapping) and ref.get("evidence_id")
            ),
        }
        for item in readiness.get("conditions", ()) or ()
        if isinstance(item, Mapping)
    ]
    return {
        "ready": bool(readiness.get("ready")),
        "armable": bool(readiness.get("armable")),
        "blocker_count": int(readiness.get("blocker_count") or 0),
        "conditions_hash": str(readiness.get("conditions_hash") or ""),
        "evaluated_at": str(readiness.get("evaluated_at") or ""),
        "valid_until": str(readiness.get("valid_until") or ""),
        "landing_state": str((landing or {}).get("state") or ""),
        "conditions": conditions,
    }


def policy_section(policy_generation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "digest": str(policy_generation.get("digest") or ""),
        "authority": str(policy_generation.get("authority") or ""),
        "layers": [
            {
                "layer": str(layer.get("layer") or ""),
                "source": str(layer.get("source") or ""),
                "digest": str(layer.get("digest") or ""),
                "state": str(layer.get("state") or ""),
            }
            for layer in policy_generation.get("layers", ()) or ()
            if isinstance(layer, Mapping)
        ],
    }


def run_plan_section(run_plan: Mapping[str, Any] | None) -> dict[str, Any]:
    if not run_plan:
        return {"digest": "", "parent_digest": "", "inputs_digest": "", "state": "no_plan"}
    if run_plan.get("error"):
        # A plan that could not be computed is recorded as such, never as "no plan".
        return {"digest": "", "parent_digest": "", "inputs_digest": "", "state": "unavailable"}
    return {
        "digest": str(run_plan.get("digest") or ""),
        "parent_digest": str(run_plan.get("parent_digest") or ""),
        "inputs_digest": str(run_plan.get("inputs_digest") or ""),
        "state": "planned" if run_plan.get("digest") else "no_plan",
    }


# --------------------------------------------------------------------------- #
# statement
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RecordContext:
    """Everything one record reads besides the decision and the signer.

    ``readiness`` is the readiness core's wire for *revision* evaluated at
    ``issued_at`` (it carries ``policy_generation``); ``run_plan`` the Snapshot's
    effective run plan wire (``None`` without a planner).
    """

    store: Any
    session: ReviewSession
    revision: ReviewRevision
    snapshot_number: int
    readiness: Mapping[str, Any]
    run_plan: Mapping[str, Any] | None
    issuer_kind: str
    issuer_id: str
    repository: str
    exported_by_id: str
    exported_by_type: str = "human"
    #: The parts and their states at the record instant (``None``: the composition cannot say).
    parts: Sequence[RecordPart] | None = None


def _parts_for(context: RecordContext, decision: ReviewDecision) -> Sequence[RecordPart] | None:
    """The parts a record may bind: only the session reviewer's own answers, only on a human outcome."""

    if context.parts is None or decision.kind != "human_outcome" or decision.actor_id != context.session.reviewer_id:
        return None
    return context.parts


def build_statement(
    context: RecordContext,
    decision: ReviewDecision,
    *,
    signing: SigningIdentity,
    issued_at: str,
    facts: RecordFacts | None = None,
    shape: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The in-toto Statement for one decided Snapshot (pure given *facts*).

    A human outcome is a v2 record; a landing decision and a provider merge stay v1
    byte for byte. *shape* is an already-signed statement being rebuilt: the rebuild
    keeps its version and the optional v2 sections it carries, so a record signed
    before a section existed still reproduces.
    """

    if parse_timestamp(issued_at) is None:
        raise RecordSchemaError("issued_at must be an aware ISO-8601 instant")
    session, revision = context.session, context.revision
    if revision.review_id != session.id:
        raise RecordSchemaError("the revision does not belong to this Review")
    rows = (
        facts
        if facts is not None
        else gather_record_facts(context.store, session, revision, as_of=issued_at, coverage_actor_id=decision.actor_id)
    )
    policy = context.readiness.get("policy_generation")
    digest: dict[str, str] = {"lemoncrowTreeFingerprint": revision.tree_fingerprint}
    if revision.head_sha:
        digest["gitCommit"] = revision.head_sha
    if shape is None:
        is_v2 = decision.kind == "human_outcome"
        wanted = {"parts", "ai_hints", "change"}
    else:
        is_v2 = shape.get("predicateType") == PREDICATE_TYPE_V2
        wanted = set(_mapping(shape.get("predicate"))) & {"parts", "ai_hints", "change"}
    predicate: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA_VERSION_V2 if is_v2 else RECORD_SCHEMA_VERSION,
        "issuer": {"kind": context.issuer_kind, "id": context.issuer_id, "service": "lemoncrow"},
        "issued_at": issued_at,
        "as_of": issued_at,
        "exported_by": {"id": context.exported_by_id, "type": context.exported_by_type},
        "signing": signing.to_wire(),
        "review": {
            "id": session.id,
            "subject_type": session.subject_type,
            "repository": context.repository,
            "source_ref": session.source_ref,
            "title": session.title,
        },
        "snapshot": snapshot_section(session, revision, number=context.snapshot_number),
        "decision": decision.to_wire(),
        "readiness": readiness_section(context.readiness),
        "policy_generation": policy_section(policy if isinstance(policy, Mapping) else {}),
        "run_plan": run_plan_section(context.run_plan),
        "evidence": evidence_section(rows),
        "waivers": waivers_section(rows),
        "judgments": judgments_section(rows),
    }
    if is_v2:
        from lemoncrow.pro.capabilities.review.seen_coverage import coverage_section

        predicate["seen_coverage"] = coverage_section(rows.coverage_events, revision_id=revision.id)
        parts = _parts_for(context, decision)
        if "parts" in wanted and parts is not None:
            predicate["parts"] = parts_section(parts)
        if "ai_hints" in wanted:
            predicate["ai_hints"] = ai_hints_section(rows, revision_id=revision.id)
        if "change" in wanted:
            predicate["change"] = change_section(revision)
    statement = {
        "_type": STATEMENT_TYPE,
        "subject": [{"name": f"lemoncrow-review/{session.id}/snapshot/{revision.id}", "digest": digest}],
        "predicateType": PREDICATE_TYPE_V2 if is_v2 else PREDICATE_TYPE,
        "predicate": predicate,
    }
    errors = validate_statement(statement)
    if errors:
        raise RecordSchemaError("; ".join(errors))
    return statement


def statement_digest(statement: Mapping[str, Any]) -> str:
    return sha256_digest(canonical_json(statement))


_PREDICATE_SECTIONS: Final[Mapping[str, type]] = {
    "schema_version": int,
    "issuer": dict,
    "issued_at": str,
    "as_of": str,
    "exported_by": dict,
    "signing": dict,
    "review": dict,
    "snapshot": dict,
    "decision": dict,
    "readiness": dict,
    "policy_generation": dict,
    "run_plan": dict,
    "evidence": list,
    "waivers": list,
    "judgments": dict,
}
_SNAPSHOT_KEYS: Final[tuple[str, ...]] = ("review_id", "revision_id", "tree_fingerprint", "head_sha", "base_sha")


def _v2_section_errors(predicate: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if "parts" in predicate:
        parts = _mapping(predicate.get("parts"))
        counts = _mapping(parts.get("counts"))

        def is_count(value: object) -> bool:
            return isinstance(value, int) and not isinstance(value, bool)

        if (
            not all(is_count(counts.get(key)) for key in ("decision", "quick_check", "accepted_by_rule"))
            or not isinstance(counts.get("rules"), Mapping)
            or not all(
                isinstance(parts.get(key), list) for key in ("decisions", "quick_check_changes", "not_confirmed")
            )
            or not is_count(parts.get("later"))
        ):
            errors.append(
                "v2 parts must carry counts, decisions, quick-check changes, unconfirmed parts and a later count"
            )
    hints = predicate.get("ai_hints")
    if "ai_hints" in predicate and (
        not isinstance(hints, Mapping)
        or hints.get("basis") != AI_HINTS_BASIS
        or not isinstance(hints.get("hints"), list)
    ):
        errors.append("v2 AI hints must state their basis and list the hints")
    change = predicate.get("change")
    if "change" in predicate and (
        not isinstance(change, Mapping)
        or not all(
            isinstance(change.get(key), str) for key in ("agent_host", "agent_session_id", "provenance_certainty")
        )
    ):
        errors.append("v2 change must name its agent host, agent session and provenance certainty")
    return errors


def validate_statement(statement: object) -> list[str]:
    """Predicate schema violations (empty when the statement is a valid v1 review record)."""

    if not isinstance(statement, Mapping):
        return ["statement is not a JSON object"]
    errors: list[str] = []
    if statement.get("_type") != STATEMENT_TYPE:
        errors.append(f"_type is not {STATEMENT_TYPE}")
    if statement.get("predicateType") not in {PREDICATE_TYPE, PREDICATE_TYPE_V2}:
        errors.append("predicateType is not a recognized review-record version")
    subject = statement.get("subject")
    if (
        not isinstance(subject, list)
        or len(subject) != 1
        or not isinstance(subject[0], Mapping)
        or not isinstance(subject[0].get("digest"), Mapping)
        or not subject[0].get("digest")
    ):
        errors.append("subject must name exactly one revision with a digest")
    predicate = statement.get("predicate")
    if not isinstance(predicate, Mapping):
        return [*errors, "predicate is not a JSON object"]
    for key, kind in _PREDICATE_SECTIONS.items():
        value = predicate.get(key)
        if not isinstance(value, kind) or (isinstance(value, bool) and kind is int):
            errors.append(f"predicate.{key} is missing or not a {kind.__name__}")
    if errors:
        return errors
    expected = (
        RECORD_SCHEMA_VERSION_V2 if statement.get("predicateType") == PREDICATE_TYPE_V2 else RECORD_SCHEMA_VERSION
    )
    if predicate["schema_version"] != expected:
        errors.append(f"unsupported predicate schema_version {predicate['schema_version']!r} for this predicate type")
    if expected == RECORD_SCHEMA_VERSION_V2:
        coverage = predicate.get("seen_coverage")
        if (
            not isinstance(coverage, Mapping)
            or coverage.get("basis") != "reviewer_reported_not_independent_evidence"
            or not isinstance(coverage.get("seen"), list)
            or not isinstance(coverage.get("skipped"), list)
        ):
            errors.append("v2 coverage must list self-reported seen and skipped parts")
        elif coverage.get("revision_id") != predicate.get("snapshot", {}).get("revision_id"):
            errors.append("v2 coverage belongs to a different revision")
        # Sections P6-1 added are optional when absent (a record signed in the P3-2 era has none)
        # and checked whenever present.
        errors.extend(_v2_section_errors(predicate))
    if parse_timestamp(predicate["issued_at"]) is None:
        errors.append("predicate.issued_at is not an aware ISO-8601 instant")
    signing = predicate["signing"]
    if signing.get("state") not in {"signed", "unsigned"} or signing.get("mode") not in SIGNING_MODES:
        errors.append("predicate.signing names an unknown state or mode")
    elif (signing.get("state") == "unsigned") != (signing.get("mode") == "unsigned"):
        errors.append("predicate.signing state and mode disagree")
    elif signing.get("state") == "unsigned" and signing.get("notice") != UNSIGNED_NOTICE:
        errors.append("an unsigned record must carry the unsigned notice")
    elif signing.get("state") == "signed" and (not signing.get("key_id") or not signing.get("algorithm")):
        errors.append("a signed record names its key id and algorithm")
    snapshot = predicate["snapshot"]
    for key in _SNAPSHOT_KEYS:
        if not isinstance(snapshot.get(key), str):
            errors.append(f"predicate.snapshot.{key} is missing")
    if not snapshot.get("review_id") or not snapshot.get("revision_id") or not snapshot.get("tree_fingerprint"):
        errors.append("predicate.snapshot does not identify the revision")
    decision = predicate["decision"]
    if decision.get("kind") not in DECISION_KINDS or not decision.get("outcome"):
        errors.append("predicate.decision names no known decision")
    actor = decision.get("actor")
    if not isinstance(actor, Mapping) or not actor.get("id") or parse_timestamp(decision.get("decided_at")) is None:
        errors.append("predicate.decision names no actor or decision time")
    for row in predicate["evidence"]:
        if not isinstance(row, Mapping) or not isinstance(row.get("executor_class"), str):
            errors.append("every evidence row names its executor class (empty when not an execution result)")
            break
        if not isinstance(row.get("attestation_class"), str):
            errors.append("every evidence row names its attestation class")
            break
    if not str(predicate["policy_generation"].get("digest") or "").startswith("policy:"):
        errors.append("predicate.policy_generation names no policy generation digest")
    readiness = predicate["readiness"]
    if not isinstance(readiness.get("conditions"), list) or not isinstance(readiness.get("ready"), bool):
        errors.append("predicate.readiness carries no readiness state")
    judgments = predicate["judgments"]
    if not isinstance(judgments.get("outcomes"), list):
        errors.append("predicate.judgments carries no outcomes")
    return errors


def readiness_consistent(readiness: Mapping[str, Any], *, policy_generation: str) -> bool:
    """Whether a recorded readiness section's ``conditions_hash`` is its own conditions' hash."""

    conditions = readiness.get("conditions")
    if not isinstance(conditions, list):
        return False
    recomputed = conditions_digest(
        [item for item in conditions if isinstance(item, Mapping)], policy_generation=policy_generation
    )
    return recomputed == str(readiness.get("conditions_hash") or "")


StoreSection = Literal["match", "mismatch", "not_checked"]


def compare_with_store(
    statement: Mapping[str, Any],
    *,
    store: Any,
    session: ReviewSession,
    revision: ReviewRevision | None,
    snapshot_number: int,
    current_policy_generation: Mapping[str, Any] | None,
    readiness_at: Mapping[str, Any] | None,
    run_plan: Mapping[str, Any] | None,
    parts_at: Sequence[RecordPart] | None = None,
) -> dict[str, str]:
    """Recompute every stored section of *statement* and say which still match.

    ``parts_at`` is the parts and their states now (``None``: not recomputable here,
    for example because the record is for an older Snapshot).

    ``readiness_at`` is the readiness core re-evaluated at the recorded
    ``evaluated_at`` (``None``: not re-evaluated). A section that changed after
    signing (a later policy edit, say) is reported as ``mismatch``, never
    silently accepted.
    """

    predicate = statement.get("predicate") if isinstance(statement, Mapping) else None
    if not isinstance(predicate, Mapping) or revision is None:
        return {"snapshot": "mismatch"}
    as_of = str(predicate.get("as_of") or "")
    recorded_decision = predicate.get("decision")
    recorded_actor = recorded_decision.get("actor") if isinstance(recorded_decision, Mapping) else None
    facts = gather_record_facts(
        store,
        session,
        revision,
        as_of=as_of,
        coverage_actor_id=str(recorded_actor.get("id") or "") if isinstance(recorded_actor, Mapping) else "",
    )

    def verdict(same: bool) -> str:
        return "match" if same else "mismatch"

    out: dict[str, str] = {
        "snapshot": verdict(predicate.get("snapshot") == snapshot_section(session, revision, number=snapshot_number)),
        "evidence": verdict(predicate.get("evidence") == evidence_section(facts)),
        "waivers": verdict(predicate.get("waivers") == waivers_section(facts)),
        "judgments": verdict(predicate.get("judgments") == judgments_section(facts)),
    }
    if predicate.get("schema_version") == RECORD_SCHEMA_VERSION_V2:
        from lemoncrow.pro.capabilities.review.seen_coverage import coverage_section

        out["seen_coverage"] = verdict(
            predicate.get("seen_coverage") == coverage_section(facts.coverage_events, revision_id=revision.id)
        )
        if "ai_hints" in predicate:
            out["ai_hints"] = verdict(predicate.get("ai_hints") == ai_hints_section(facts, revision_id=revision.id))
        if "change" in predicate:
            out["change"] = verdict(predicate.get("change") == change_section(revision))
        if "parts" in predicate:
            same_reviewer = isinstance(recorded_actor, Mapping) and recorded_actor.get("id") == session.reviewer_id
            out["parts"] = (
                "not_checked"
                if parts_at is None or not same_reviewer
                else verdict(predicate.get("parts") == parts_section(parts_at))
            )
    recorded_policy = predicate.get("policy_generation")
    recorded_digest = str(recorded_policy.get("digest") or "") if isinstance(recorded_policy, Mapping) else ""
    if current_policy_generation is None:
        out["policy_generation"] = "not_checked"
    else:
        out["policy_generation"] = verdict(recorded_policy == policy_section(current_policy_generation))
    readiness = predicate.get("readiness")
    if not isinstance(readiness, Mapping) or not readiness_consistent(readiness, policy_generation=recorded_digest):
        out["readiness"] = "mismatch"
    elif readiness_at is None:
        out["readiness"] = "not_checked"
    else:
        out["readiness"] = verdict(readiness == readiness_section(readiness_at))
    out["run_plan"] = verdict(predicate.get("run_plan") == run_plan_section(run_plan))
    decision = predicate.get("decision")
    if isinstance(decision, Mapping) and decision.get("kind") == "human_outcome":
        reference = str(decision.get("reference") or "")
        actor = decision.get("actor") if isinstance(decision.get("actor"), Mapping) else {}
        row = next((item for item in facts.outcomes if item.id == reference), None)
        out["decision"] = verdict(
            row is not None
            and row.revision_id == revision.id
            and _DECISION_OUTCOMES.get(row.outcome) == decision.get("outcome")
            and row.reviewer_id == (actor or {}).get("id")
            and row.created_at == decision.get("decided_at")
        )
    elif isinstance(decision, Mapping) and decision.get("kind") == "landing":
        out["decision"] = verdict(_landing_decision_stored(store, session, revision, decision))
    else:
        out["decision"] = "not_checked"
    return out


def _landing_decision_stored(
    store: Any, session: ReviewSession, revision: ReviewRevision, decision: Mapping[str, Any]
) -> bool:
    """Does the store still hold the landing decision a record names (P10-LAND)?"""

    from lemoncrow.pro.capabilities.review import break_glass
    from lemoncrow.pro.capabilities.review.auto_approval import auto_approval_relationship
    from lemoncrow.pro.capabilities.review.landing import landing_metadata

    reference = str(decision.get("reference") or "")
    detail = _mapping(decision.get("detail"))
    actor = _mapping(decision.get("actor"))
    if decision.get("outcome") == "auto_approved":
        approval = auto_approval_relationship(store, session.id)
        metadata = landing_metadata(approval)
        return (
            approval is not None
            and approval.id == reference
            and str(metadata.get("revision_id") or "") == revision.id
            and str(metadata.get("decision_digest") or "") == str(detail.get("decision_digest") or "")
        )
    if decision.get("outcome") == "break_glass":
        record = next((row for row in break_glass.records(store, [revision.id]) if row.get("id") == reference), None)
        return (
            record is not None
            and str(record.get("actor_id") or "") == str(actor.get("id") or "")
            and break_glass.decision_detail(record) == {str(key): str(value) for key, value in detail.items()}
        )
    return False


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def summarize(statement: Mapping[str, Any]) -> dict[str, Any]:
    """Display facts of a statement (never a verification verdict)."""

    predicate = _mapping(statement.get("predicate"))
    snapshot = _mapping(predicate.get("snapshot"))
    decision = _mapping(predicate.get("decision"))
    signing = _mapping(predicate.get("signing"))
    policy = _mapping(predicate.get("policy_generation"))
    readiness = _mapping(predicate.get("readiness"))
    actor = _mapping(decision.get("actor"))
    parts = predicate.get("parts")
    decisions = _mapping(parts).get("decisions") if isinstance(parts, Mapping) else None
    return {
        # How many decisions the reviewer answered one by one (None: this record binds no parts).
        "parts_reviewed_one_by_one": (
            sum(
                1 for row in decisions if isinstance(row, Mapping) and row.get("state") in {"reviewed", "needs_changes"}
            )
            if isinstance(decisions, list)
            else None
        ),
        "review_id": str(snapshot.get("review_id") or ""),
        "revision_id": str(snapshot.get("revision_id") or ""),
        "snapshot_number": snapshot.get("number"),
        "head_sha": str(snapshot.get("head_sha") or ""),
        "decision": str(decision.get("outcome") or ""),
        "decision_kind": str(decision.get("kind") or ""),
        "decided_by": str(actor.get("id") or ""),
        "decided_at": str(decision.get("decided_at") or ""),
        "issued_at": str(predicate.get("issued_at") or ""),
        "signing_state": str(signing.get("state") or ""),
        "signing_mode": str(signing.get("mode") or ""),
        "key_id": str(signing.get("key_id") or ""),
        "policy_generation": str(policy.get("digest") or ""),
        "ready": readiness.get("ready") is True,
    }


def relationship_rows(store: Any, review_id: str) -> Sequence[Any]:
    return tuple(store.list_relationships(review_id, kind=RECORD_RELATIONSHIP_KIND))


def iter_waiver_keys(statement: Mapping[str, Any]) -> Iterable[str]:
    predicate = _mapping(statement.get("predicate"))
    for row in predicate.get("waivers", ()) or ():
        if isinstance(row, Mapping):
            yield str(row.get("requirement_key") or "")


def cited_evidence_ids(statement: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Every evidence id a statement cites, with where: ``evidence`` or ``condition:<id>``.

    The two places :func:`evidence_section` and :func:`readiness_section` write
    evidence identity, read back for the trust incident runbook (Gate T-J): a
    v1 record names reused results only by evidence id.
    """

    predicate = _mapping(statement.get("predicate"))
    places: dict[str, list[str]] = {}
    for row in predicate.get("evidence", ()) or ():
        if isinstance(row, Mapping) and row.get("id"):
            places.setdefault(str(row["id"]), []).append("evidence")
    for condition in _mapping(predicate.get("readiness")).get("conditions", ()) or ():
        if not isinstance(condition, Mapping):
            continue
        for evidence_id in condition.get("evidence_ids", ()) or ():
            if evidence_id:
                places.setdefault(str(evidence_id), []).append(f"condition:{condition.get('id') or ''}")
    return {key: tuple(dict.fromkeys(value)) for key, value in places.items()}


__all__ = [
    "AI_HINTS_BASIS",
    "DECISION_KINDS",
    "PAYLOAD_TYPE",
    "PREDICATE_TYPE",
    "RECORD_RELATIONSHIP_KIND",
    "RECORD_SCHEMA_VERSION",
    "SIGNING_MODES",
    "STATEMENT_TYPE",
    "UNSIGNED",
    "UNSIGNED_NOTICE",
    "DecisionKind",
    "RecordContext",
    "RecordFacts",
    "RecordPart",
    "RecordSchemaError",
    "ReviewDecision",
    "SigningIdentity",
    "SigningMode",
    "ai_hints_section",
    "build_statement",
    "canonical_json",
    "change_section",
    "cited_evidence_ids",
    "compare_with_store",
    "current_decision",
    "evidence_section",
    "gather_record_facts",
    "human_decision_for",
    "judgments_section",
    "parts_section",
    "policy_section",
    "readiness_consistent",
    "readiness_section",
    "run_plan_section",
    "sha256_digest",
    "snapshot_section",
    "statement_digest",
    "summarize",
    "validate_statement",
    "waivers_section",
]
