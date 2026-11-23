"""Unified validity reasons (Trust T1): one explanatory language over existing Review state.

This module owns no state. It owns the typed reason vocabulary and adapters that read
the outcome an existing authority already decided:

* human judgments: :func:`revisions.reconcile` records a reason code per mark;
* evidence freshness: :func:`freshness.assess_freshness` records a reason code;
* cross-Snapshot check carry: :func:`incremental.explain_carry`;
* typed check equivalence: :meth:`check_ingest.EquivalenceKey.mismatches`;
* readiness: the ``reason`` and cited evidence of a :class:`readiness.ReadinessCondition`.

A :class:`ValidityResult` explains; it never decides. Every reason code implies exactly
one state, so the same inputs always produce the same reason chain, and an unknown code
is refused rather than shown. The existing state contracts (mark states, freshness
states, readiness condition states) are unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.freshness import EvidenceFreshness
    from lemoncrow.pro.capabilities.review.incremental import CarryDecision
    from lemoncrow.pro.capabilities.review.readiness import ReadinessCondition
    from lemoncrow.pro.capabilities.review.revisions import Reconciliation

ValidityState = Literal["preserved", "reopened", "reused", "reverify", "expired", "invalid", "unknown"]

VALIDITY_STATES: Final[tuple[ValidityState, ...]] = (
    "preserved",
    "reopened",
    "reused",
    "reverify",
    "expired",
    "invalid",
    "unknown",
)


@dataclass(frozen=True, slots=True)
class ValidityReason:
    """One registered reason: the state it implies and the sentence a person reads."""

    code: str
    state: ValidityState
    summary: str


def _reasons(*rows: tuple[str, ValidityState, str]) -> dict[str, ValidityReason]:
    out: dict[str, ValidityReason] = {}
    for code, state, summary in rows:
        if code in out:
            raise ValueError(f"duplicate validity reason {code!r}")
        out[code] = ValidityReason(code, state, summary)
    return out


REASONS: Final[Mapping[str, ValidityReason]] = _reasons(
    # Human judgments (revisions.reconcile).
    ("content_unchanged", "preserved", "the content matches what the reviewer judged"),
    ("human_request_kept", "preserved", "a requested change stays until a person resolves it"),
    (
        "base_only_equivalent",
        "preserved",
        "only the diff base moved under a patch-equivalent change, so the judgment is kept",
    ),
    (
        "deleted_text_no_longer_shown",
        "preserved",
        "the part shows the same body and no longer shows deleted lines the reviewer judged with it, so the judgment is kept",
    ),
    ("fingerprint_restored", "preserved", "the content is identifiable again and matches what the reviewer judged"),
    (
        "behavior_mark_kept",
        "preserved",
        "a behavior judgment is kept; the behavior target's fingerprint decides on read whether it still holds",
    ),
    ("no_attestation", "preserved", "the mark claimed nothing about the content, so nothing is invalidated"),
    ("renamed", "preserved", "the part moved with a file rename"),
    (
        "symbol_moved",
        "preserved",
        "the symbol moved to another file with exactly the same name and body, and nothing else matches it",
    ),
    (
        "symbol_renamed",
        "reopened",
        "the symbol has what looks like the same body under a different name, so the judgment reopens; this is not proof the change is routine",
    ),
    ("content_changed", "reopened", "the content differs from what the reviewer judged"),
    (
        "part_order_changed",
        "reopened",
        "the part has the same content but its position among the file's unchanged base lines changed, and in a shell or Make file the order decides what runs",
    ),
    (
        "part_place_unverifiable",
        "reopened",
        "the part has the same content, but nothing recorded shows it still sits where the reviewer saw it in its shell or Make file (the file is too large or unreadable to check and it changed, or the verdict predates place checks), so it is read",
    ),
    (
        "hunks_renumbered",
        "reopened",
        "the file's hunk count changed and hunk identity is ordinal, so hunk judgments reset",
    ),
    ("fingerprint_unknown", "unknown", "the content cannot be fingerprinted, so it cannot stay reviewed"),
    ("unit_removed", "invalid", "the part is not in this revision"),
    # Evidence freshness (freshness.assess_freshness).
    ("snapshot_bound", "preserved", "the evidence is bound to its revision; the revision binding decides staleness"),
    ("observation_current", "preserved", "the external observation is inside its validity window"),
    ("observation_expired", "expired", "the external observation passed its validity window"),
    (
        "freshness_policy_unreadable",
        "expired",
        "the evidence freshness policy could not be read, so the observation is not trusted",
    ),
    ("observation_time_unknown", "unknown", "the observation has no readable source timestamp"),
    ("valid_until_unreadable", "unknown", "the observation's valid_until is unreadable"),
    ("evidence_not_on_snapshot", "unknown", "the cited evidence is not on this revision"),
    # Cross-Snapshot check carry (incremental.explain_carry).
    (
        "carry_exact_key",
        "reused",
        "an earlier revision ran this check with exactly the same carry key in the same executor environment",
    ),
    ("replayed_on_snapshot", "reused", "this revision already has this exact attempt's result, so it is replayed"),
    ("fresh_run_requested", "reverify", "a fresh run was requested, so nothing is reused"),
    ("carry_disabled", "reverify", "carrying checks forward is turned off on this server, so the check runs again"),
    ("carry_not_plain_run", "reverify", "only a plain head run is carried forward"),
    (
        "carry_no_input_digest",
        "reverify",
        "the change list is incomplete or unreadable, so the run has no input digest to match",
    ),
    ("carry_no_earlier_snapshot", "reverify", "there is no earlier revision of this Review"),
    ("carry_no_prior_run", "reverify", "no earlier revision has a carriable run of this check"),
    ("carry_prior_key_unrecorded", "reverify", "the earlier run predates recorded carry keys"),
    ("carry_policy_changed", "reverify", "the trusted policy changed since the earlier run"),
    ("carry_recipe_changed", "reverify", "the check recipe changed since the earlier run"),
    ("carry_executor_changed", "reverify", "the executor class changed since the earlier run"),
    ("carry_scope_changed", "reverify", "the working directory or the selected tests changed since the earlier run"),
    ("carry_attempt_differs", "reverify", "the earlier run is another attempt"),
    ("carry_inputs_changed", "reverify", "a file inside the tests' input closure changed since the earlier run"),
    ("carry_evidence_unverifiable", "reverify", "the earlier run's evidence is missing or does not verify"),
    (
        "carry_environment_unknown",
        "reverify",
        "the executor environment of this run or of the earlier run is not fully known, so nothing is carried forward",
    ),
    (
        "carry_environment_changed",
        "reverify",
        "the executor environment (image, kernel, the executables the check runs, or sandbox) changed since the"
        " earlier run",
    ),
    # Typed check equivalence (check_ingest.EquivalenceKey).
    ("equivalent_result", "reused", "a current result of the same work satisfies the requirement"),
    ("equivalence_disabled", "reverify", "CI equivalence is turned off on this server, so no other result stands in"),
    ("snapshot_not_reusable", "reverify", "the revision is uncommitted or has no head commit"),
    ("no_current_result", "reverify", "no current result of this check is on the revision"),
    ("equivalence_head_changed", "reverify", "the result is for another head commit"),
    ("equivalence_tool_differs", "reverify", "the result is from another check"),
    ("equivalence_recipe_differs", "reverify", "the result covers another recipe"),
    ("equivalence_recipe_digest_differs", "reverify", "the result's recipe digest differs from the required one"),
    ("equivalence_config_differs", "reverify", "the result's configuration digest differs from the required one"),
    ("attestation_class_insufficient", "invalid", "the result's attestation class cannot satisfy a requirement"),
    ("result_not_completed", "reverify", "the result is not a completed PASS or FAIL"),
    # Readiness (readiness.ReadinessCondition).
    (
        "policy_generation_changed",
        "reverify",
        "decisions were taken under an older policy generation and the current policy is not satisfied",
    ),
    (
        "policy_generation_revalidated",
        "preserved",
        "decisions taken under an older policy generation still satisfy the current policy",
    ),
    (
        "condition_evidence_expired",
        "expired",
        "a pass relied on an observation that is no longer current, so it is not counted",
    ),
)


@dataclass(frozen=True, slots=True)
class ValidityResult:
    """Why a prior conclusion does or does not apply now. Explanatory only.

    ``reason_code`` decides ``state`` (the registry maps each code to one state);
    ``causes`` are further registered codes, most decisive first, so ``chain``
    reads as the full deterministic reason chain.
    """

    state: ValidityState
    reason_code: str
    subject_ref: str
    source_revision: str = ""
    current_revision: str = ""
    evidence_refs: tuple[str, ...] = ()
    detail: str = ""
    causes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        reason = REASONS.get(self.reason_code)
        if reason is None:
            raise ValueError(f"unregistered validity reason {self.reason_code!r}")
        if self.state != reason.state:
            raise ValueError(f"validity reason {self.reason_code!r} implies {reason.state!r}, not {self.state!r}")
        unknown = [code for code in self.causes if code not in REASONS]
        if unknown:
            raise ValueError(f"unregistered validity causes {unknown!r}")
        if not self.subject_ref:
            raise ValueError("a validity result names its subject")

    @property
    def chain(self) -> tuple[str, ...]:
        return (self.reason_code, *self.causes)

    def to_wire(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason_code": self.reason_code,
            "chain": list(self.chain),
            "subject_ref": self.subject_ref,
            "source_revision": self.source_revision,
            "current_revision": self.current_revision,
            "evidence_refs": list(self.evidence_refs),
            "detail": self.detail or REASONS[self.reason_code].summary,
        }


def result(
    code: str,
    subject_ref: str,
    *,
    causes: Iterable[str] = (),
    source_revision: str = "",
    current_revision: str = "",
    evidence_refs: Iterable[str] = (),
    detail: str = "",
) -> ValidityResult:
    """A result whose state is the one ``code`` implies."""

    reason = REASONS.get(code)
    if reason is None:
        raise ValueError(f"unregistered validity reason {code!r}")
    return ValidityResult(
        state=reason.state,
        reason_code=code,
        subject_ref=subject_ref,
        source_revision=source_revision,
        current_revision=current_revision,
        evidence_refs=tuple(evidence_refs),
        detail=detail,
        causes=tuple(causes),
    )


def summary(code: str) -> str:
    reason = REASONS.get(code)
    if reason is None:
        raise ValueError(f"unregistered validity reason {code!r}")
    return reason.summary


# --------------------------------------------------------------------------- #
# adapters
# --------------------------------------------------------------------------- #


def mark_subject(reviewer_id: str, unit_key: str) -> str:
    return f"mark:{reviewer_id}:{unit_key}"


_SYMBOL_MOVE_LEADS: Final[Mapping[str, str]] = {"moved": "symbol moved from ", "renamed": "symbol renamed from "}


def mark_event_reason(code: str, *, renamed: bool = False, symbol_move: tuple[str, str, str] | None = None) -> str:
    """The audited reconciliation reason persisted with a mark event.

    *symbol_move* is ``("moved" | "renamed", previous path, previous qualified name)``. It is
    persisted as a JSON tail after a fixed lead so :func:`parse_symbol_move_reason` can read the
    provenance back from the event log without any new column.
    """

    text = f"revision reconciliation: {summary(code)}"
    if symbol_move is not None:
        kind, path, symbol = symbol_move
        return f"{_SYMBOL_MOVE_LEADS[kind]}{json.dumps([path, symbol])}; {text}"
    return f"part identity migrated across rename; {text}" if renamed else text


def parse_symbol_move_reason(reason: str) -> tuple[str, str, str] | None:
    """``(kind, previous path, previous qualified name)`` from a mark event reason, or ``None``."""

    for kind, lead in _SYMBOL_MOVE_LEADS.items():
        if reason.startswith(lead):
            try:
                payload, _end = json.JSONDecoder().raw_decode(reason[len(lead) :])
            except ValueError:
                return None
            if isinstance(payload, list) and len(payload) == 2 and all(isinstance(item, str) for item in payload):
                return (kind, payload[0], payload[1])
    return None


def mark_results(reconciliation: Reconciliation, *, current_revision: str = "") -> tuple[ValidityResult, ...]:
    """One result per mark the reconciliation decided, in a stable order.

    A dropped row whose unit moved with a rename is the old key of a migrated
    mark (its surviving mark explains it), so only a genuinely removed unit
    yields ``unit_removed``.
    """

    codes = {(reviewer, key): code for reviewer, key, code in reconciliation.reasons}
    followed = {(move.old_key, move.new_key) for move in reconciliation.moves}
    moved_from = {new: old for old, new in reconciliation.aliases if (old, new) not in followed}
    out: list[ValidityResult] = []
    for mark in reconciliation.marks:
        code = codes.get((mark.reviewer_id, mark.unit_key))
        if code is None:
            raise ValueError(f"reconciliation recorded no reason for {mark.unit_key!r}")
        renamed = mark.unit_key in moved_from
        out.append(
            result(
                code,
                mark_subject(mark.reviewer_id, mark.unit_key),
                causes=("renamed",) if renamed and code != "renamed" else (),
                source_revision=mark.reviewed_revision_id,
                current_revision=current_revision,
            )
        )
    # A file rename moves every reviewer's marks; a symbol move only the reviewers whose mark followed it.
    aliased = {old for old, _new in reconciliation.aliases if (old, _new) not in followed}
    followed_by = {(move.reviewer_id, move.old_key) for move in reconciliation.moves}
    for mark in reconciliation.dropped:
        if mark.unit_key in aliased or (mark.reviewer_id, mark.unit_key) in followed_by:
            continue
        out.append(
            result(
                "unit_removed",
                mark_subject(mark.reviewer_id, mark.unit_key),
                source_revision=mark.reviewed_revision_id,
                current_revision=current_revision,
            )
        )
    return tuple(sorted(out, key=lambda item: item.subject_ref))


def freshness_result(freshness: EvidenceFreshness) -> ValidityResult:
    """The validity of one evidence row at the instant its freshness was assessed."""

    return result(
        freshness.reason_code,
        f"evidence:{freshness.evidence_id}",
        source_revision=freshness.source_revision,
        evidence_refs=(freshness.evidence_id,),
        detail=freshness.reason,
    )


def carry_result(decision: CarryDecision, *, subject_ref: str, current_revision: str = "") -> ValidityResult:
    """Why a head check run was carried from an earlier Snapshot, or why it runs again."""

    codes = decision.reasons or ("carry_no_prior_run",)
    return result(
        codes[0],
        subject_ref,
        causes=codes[1:],
        source_revision=decision.origin_revision_id,
        current_revision=current_revision,
        evidence_refs=(decision.evidence_id,) if decision.evidence_id else (),
    )


def equivalence_result(
    reasons: Iterable[str],
    *,
    subject_ref: str,
    evidence_id: str = "",
    current_revision: str = "",
) -> ValidityResult:
    """Whether a current typed result stands in for a required one (``check_ingest.explain_equivalent``)."""

    codes = list(reasons) or ["equivalent_result"]
    return result(
        codes[0],
        subject_ref,
        causes=codes[1:],
        current_revision=current_revision,
        evidence_refs=(evidence_id,) if evidence_id else (),
    )


def condition_results(condition: ReadinessCondition, *, current_revision: str = "") -> tuple[ValidityResult, ...]:
    """The validity statements an evaluated readiness condition carries: its own, then each cited row's.

    Only validity reasons are mapped (a policy generation change, an expired
    pass); ordinary progress reasons such as ``awaiting_human`` or ``not_run``
    say where work stands, not whether an earlier conclusion still applies.
    A cited row the freshness guard has not resolved yet says nothing.
    """

    refs = [(ref, ref.reason_code) for ref in condition.evidence_refs if ref.reason_code]
    subject = f"condition:{condition.id}"
    out: list[ValidityResult] = []
    if condition.reason == "policy_changed":
        out.append(
            result("policy_generation_changed", subject, current_revision=current_revision, detail=condition.detail)
        )
    elif condition.reason == "revalidated":
        out.append(
            result("policy_generation_revalidated", subject, current_revision=current_revision, detail=condition.detail)
        )
    elif condition.reason == "evidence_expired":
        stale = [code for ref, code in refs if REASONS[code].state in {"expired", "unknown"}]
        out.append(
            result(
                "condition_evidence_expired",
                subject,
                causes=sorted(set(stale)),
                current_revision=current_revision,
                evidence_refs=[ref.evidence_id for ref, code in refs if code in stale],
                detail=condition.detail,
            )
        )
    for ref, code in refs:
        out.append(
            result(
                code,
                f"evidence:{ref.evidence_id}",
                source_revision=ref.source_revision,
                current_revision=current_revision,
                evidence_refs=(ref.evidence_id,),
                detail=ref.reason,
            )
        )
    return tuple(out)


__all__ = [
    "REASONS",
    "VALIDITY_STATES",
    "ValidityReason",
    "ValidityResult",
    "ValidityState",
    "carry_result",
    "condition_results",
    "equivalence_result",
    "freshness_result",
    "mark_event_reason",
    "mark_results",
    "mark_subject",
    "parse_symbol_move_reason",
    "result",
    "summary",
]
