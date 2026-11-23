"""Second reviewer: asks, status, answers hidden until given, and the signature (SPEC 4.16, P7-1).

Hosted only (SPEC 7.1 P0-2). A composition that knows people (their CODEOWNERS, their names, who may sign)
passes a :class:`SecondReviewerAuthority` to the Review API; without one (local, the default) none of the
routes below exist and no payload carries a second-reviewer field.

What is stored: the *ask* (a ``second_signature`` relationship per first-reviewer approval). A signature is
never stored here: the second reviewer's answers are their own marks, their verdict is their own outcome row,
and the signature is the signed record of that verdict. Counts are made only from records that verify.

The anti-anchoring rule is enforced in :func:`build_status`, the one place the first reviewer's answers
leave the server for a second reviewer: until the second reviewer has answered every part they were asked,
the payload carries no answer and no note of the first reviewer.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Request

from lemoncrow.pro.capabilities.review import viewer_lens
from lemoncrow.pro.capabilities.review.freshness import parse_timestamp
from lemoncrow.pro.capabilities.review.second_reviewer import (
    ANSWER_STATES,
    SIGNING_OUTCOMES,
    DecisionPart,
    OwnerRef,
    SecondPlan,
    agrees_with,
    answered,
    first_answers_visible,
    plan_second_reviewer,
    second_state,
    settled_verdict,
    signature_counts,
)
from lemoncrow.pro.capabilities.review.session_models import (
    ReviewOutcome,
    ReviewRelationship,
    ReviewRevision,
    ReviewSession,
)
from lemoncrow.pro.capabilities.review.store import utc_now

ASK_KIND = "second_signature"
ASK_RELATION = "co_sign"
ASK_WAITING = "waiting"
#: Someone is signing right now: the ask is claimed so a second request cannot record a second verdict.
ASK_SIGNING = "signing"
ASK_SIGNED = "signed"
ASK_WITHDRAWN = "withdrawn"
#: Still owed: a signature is asked for and has not been given.
OWED = (ASK_WAITING, ASK_SIGNING)
#: A claim older than this is abandoned (the process died mid-signature) and may be taken again.
SIGNING_STALE_SECONDS = 120
#: Stated in the record's decision detail so a verifier can tell which approval a second signature is for.
DETAIL_SECOND_OF = "second_signature_of"


class SecondReviewerAuthority(Protocol):
    """What only a composition that knows its people can say. Hosted implements it; local never does."""

    def viewer_id(self) -> str:
        """The authenticated principal of this request (fail closed: raise when none is bound)."""

    def barred_ids(self, session: ReviewSession) -> Sequence[str]:
        """The author(s) and authoring agents of the change: never a second reviewer."""

    def owners(
        self, session: ReviewSession, revision: ReviewRevision, paths: Sequence[str]
    ) -> Mapping[str, Sequence[OwnerRef]]:
        """The CODEOWNERS (people who can sign) of each changed path, read at the trusted base."""

    def person(self, session: ReviewSession, principal_id: str) -> OwnerRef:
        """How to name one principal (a name and a handle when known)."""

    def signing_unavailable(self, session: ReviewSession) -> str:
        """Why records cannot be signed here (``""`` when they can): no signature, no second signature."""

    def sign(self, review_id: str, outcome_id: str, detail: Mapping[str, str]) -> Mapping[str, Any]:
        """Sign the record of one stored verdict as the organization; ``{"status": "recorded"}`` when it did."""


# --------------------------------------------------------------------------- #
# the ask (a relationship row)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Ask:
    relationship: ReviewRelationship
    revision_id: str
    first_outcome_id: str
    first_reviewer_id: str
    second_reviewer_id: str
    area: str
    unit_keys: tuple[str, ...]
    state: str
    second_outcome_id: str = ""
    requested_at: str = ""
    signed_at: str = ""
    #: The second reviewer's disagreement, published as a must-fix note on the Review (``""``: none).
    note_annotation_id: str = ""


def _parse(row: ReviewRelationship) -> Ask | None:
    try:
        meta = json.loads(row.metadata_json or "{}")
    except ValueError:
        return None
    if not isinstance(meta, dict):
        return None
    keys = meta.get("unit_keys")
    return Ask(
        relationship=row,
        revision_id=str(meta.get("revision_id") or ""),
        first_outcome_id=row.external_id,
        first_reviewer_id=str(meta.get("first_reviewer_id") or ""),
        second_reviewer_id=str(meta.get("second_reviewer_id") or ""),
        area=str(meta.get("area") or ""),
        unit_keys=tuple(str(item) for item in keys) if isinstance(keys, list) else (),
        state=row.status,
        second_outcome_id=str(meta.get("second_outcome_id") or ""),
        requested_at=str(meta.get("requested_at") or row.created_at),
        signed_at=str(meta.get("signed_at") or ""),
        note_annotation_id=str(meta.get("note_annotation_id") or ""),
    )


def list_asks(store: Any, review_id: str) -> tuple[Ask, ...]:
    """Every ask on a Review, newest first."""

    parsed = [ask for row in store.list_relationships(review_id, kind=ASK_KIND) if (ask := _parse(row)) is not None]
    return tuple(sorted(parsed, key=lambda ask: (ask.requested_at, ask.first_outcome_id), reverse=True))


def waiting_asks_by_review(store: Any, viewer_id: str = "") -> dict[str, tuple[Ask, ...]]:
    """Every ask still owed in a store, by Review (one read for an Inbox over many Reviews).

    An ask counts only while the first reviewer's current decision on its revision is the approval that made it,
    so a superseded or withdrawn ask never keeps a "Needs your second signature" row. With *viewer_id*, only the asks
    that person owes: the others are dropped before their first reviewer's decision is looked up. Only asks in an
    owed state are read at all, so a store's history of signed and withdrawn asks costs nothing here."""

    grouped: dict[str, list[Ask]] = {}
    for row in store.list_relationships_by_kind(ASK_KIND, statuses=OWED):
        ask = _parse(row)
        # lc-debt: an ask row whose metadata cannot be read is skipped, so nothing is hidden for it (only `request_ask`
        # writes them, as JSON, so this needs outside corruption); to fail closed, hide everyone but the reader's own rows
        # on that Review, which the lens cannot say yet (it names the hidden people). SPEC 7.1 P9-4 A, follow-up.
        if ask is None or ask.state not in OWED or (viewer_id and ask.second_reviewer_id != viewer_id):
            continue
        current = _current_signing_outcome(store, row.review_id, ask.first_reviewer_id, ask.revision_id)
        if current is not None and current.id == ask.first_outcome_id:
            grouped.setdefault(row.review_id, []).append(ask)
    return {review_id: tuple(asks) for review_id, asks in grouped.items()}


def ask_answered(ask: Ask, marks: Mapping[str, str]) -> bool:
    """Whether the second reviewer has answered every part this ask named. The one rule behind "hidden until she has".

    An ask that names no part is never answered: nothing was given, so nothing is lifted."""

    return bool(ask.unit_keys) and all(marks.get(key) in ANSWER_STATES for key in ask.unit_keys)


def hidden_by_review(store: Any, viewer_id: str) -> dict[str, frozenset[str]]:
    """Whose work this viewer may not read yet, on every Review of one store where she owes a second signature.

    The bulk form of :func:`hidden_authors` for an Inbox over many Reviews: one read of the asks she owes, and her own
    marks only for those Reviews. Store-only, so an Inbox needs no Review API app per Review."""

    hidden: dict[str, set[str]] = {}
    for review_id, asks in waiting_asks_by_review(store, viewer_id).items():
        mine = _marks(store, review_id, viewer_id)
        for ask in asks:
            if not ask_answered(ask, mine):
                hidden.setdefault(review_id, set()).add(ask.first_reviewer_id)
    return {review_id: frozenset(authors) for review_id, authors in hidden.items()}


def request_ask(
    store: Any,
    *,
    review_id: str,
    revision_id: str,
    first_outcome_id: str,
    first_reviewer_id: str,
    second_reviewer_id: str,
    area: str,
    unit_keys: Sequence[str],
) -> Ask:
    """Ask *second_reviewer_id* to co-sign the approval *first_outcome_id*. Idempotent per approval."""

    if not (review_id and revision_id and first_outcome_id and first_reviewer_id and second_reviewer_id):
        raise ValueError("a second-signature ask names the review, revision, approval and both people")
    if second_reviewer_id == first_reviewer_id:
        raise ValueError("the first reviewer cannot be their own second reviewer")
    for existing in list_asks(store, review_id):
        if existing.first_outcome_id == first_outcome_id:
            return existing
    now = utc_now()
    row = store.upsert_relationship(
        ReviewRelationship(
            id="",
            review_id=review_id,
            kind=ASK_KIND,
            relation=ASK_RELATION,
            external_id=first_outcome_id,
            title=area[:200],
            status=ASK_WAITING,
            source="lemoncrow",
            metadata_json=json.dumps(
                {
                    "revision_id": revision_id,
                    "first_reviewer_id": first_reviewer_id,
                    "second_reviewer_id": second_reviewer_id,
                    "area": area,
                    "unit_keys": sorted(set(unit_keys)),
                    "requested_at": now,
                },
                sort_keys=True,
            ),
        )
    )
    ask = _parse(row)
    if ask is None:  # pragma: no cover - the row was just written by this function
        raise RuntimeError("second-signature ask unreadable after it was written")
    return ask


def _cas(store: Any, ask: Ask, status: str, **meta: Any) -> Ask | None:
    merged = json.loads(ask.relationship.metadata_json or "{}")
    merged.update(meta)
    row = store.update_relationship_if_unchanged(
        ask.relationship, status=status, metadata_json=json.dumps(merged, sort_keys=True)
    )
    return None if row is None else _parse(row)


def _stale(ask: Ask) -> bool:
    meta = json.loads(ask.relationship.metadata_json or "{}")
    since = parse_timestamp(str(meta.get("signing_since") or ""))
    return since is None or (datetime.now(UTC) - since).total_seconds() > SIGNING_STALE_SECONDS


def _since(ask: Ask) -> str:
    return str(json.loads(ask.relationship.metadata_json or "{}").get("signing_since") or "")


def _void(store: Any, session: ReviewSession, failed_id: str) -> None:
    """Set aside the one verdict a failed signing attempt recorded (*failed_id*), and nothing else.

    A void verdict is read by nobody, as if it had never been recorded, so the person's standing word is whatever
    the normal rules say without it: a real earlier decision comes back by itself, a retracted one stays retracted,
    and one recorded since the ask stays held."""

    try:
        store.void_outcome(session.id, failed_id)
    except Exception:  # the verdict stays recorded; the held rule still keeps it from counting as a signature
        logging.getLogger(__name__).warning("could not set aside an unsigned verdict on %s", session.id, exc_info=True)


def claim_ask(store: Any, ask: Ask) -> Ask | None:
    """``waiting`` (or an abandoned ``signing``) to ``signing``, compare-and-set: one request signs, the rest get ``None``."""

    if ask.state == ASK_WAITING or (ask.state == ASK_SIGNING and _stale(ask)):
        return _cas(store, ask, ASK_SIGNING, signing_since=utc_now())
    return None


def release_ask(store: Any, ask: Ask) -> Ask | None:
    """``signing`` back to ``waiting`` when the signature could not be given; nothing was given."""

    return _cas(store, ask, ASK_WAITING) if ask.state == ASK_SIGNING else None


def complete_ask(store: Any, ask: Ask, *, second_outcome_id: str) -> Ask | None:
    """``signing`` to ``signed`` (compare-and-set); ``None`` when the ask changed meanwhile or was not being signed."""

    if ask.state != ASK_SIGNING or not second_outcome_id:
        return None
    return _cas(store, ask, ASK_SIGNED, second_outcome_id=second_outcome_id, signed_at=utc_now())


def withdraw_asks(
    store: Any,
    review_id: str,
    *,
    revision_id: str,
    first_reviewer_id: str = "",
    keep_first_outcome_id: str = "",
) -> int:
    """The closer. A waiting ask no longer stands when its revision is not the latest, or when its own first
    reviewer has given a different decision since. Nobody else's verdict closes it. A signed or being-signed
    ask is history and stays."""

    closed = 0
    for ask in list_asks(store, review_id):
        if ask.state != ASK_WAITING:
            continue
        superseded = ask.revision_id != revision_id or (
            first_reviewer_id != ""
            and ask.first_reviewer_id == first_reviewer_id
            and ask.first_outcome_id != keep_first_outcome_id
        )
        if superseded and _cas(store, ask, ASK_WITHDRAWN) is not None:
            closed += 1
    return closed


def withdraw_unstanding_asks(deps: SecondDeps, session: ReviewSession) -> int:
    """After a retraction: close every waiting ask whose first reviewer's approval no longer stands."""

    closed = 0
    for ask in list_asks(deps.store, session.id):
        if ask.state != ASK_WAITING:
            continue
        current = _current_signing_outcome(deps.store, session.id, ask.first_reviewer_id, ask.revision_id)
        if (current is None or current.id != ask.first_outcome_id) and _cas(deps.store, ask, ASK_WITHDRAWN) is not None:
            closed += 1
    return closed


def effective_ask(store: Any, review_id: str, revision_id: str) -> Ask | None:
    """The ask that stands on this revision, read-only: made for this revision, and the first reviewer's current
    decision on it is still the approval that made it (so a changed verdict leaves nothing standing, signed or not)."""

    for ask in list_asks(store, review_id):  # newest first
        if ask.revision_id != revision_id or ask.state not in (*OWED, ASK_SIGNED):
            continue
        current = _current_signing_outcome(store, review_id, ask.first_reviewer_id, revision_id)
        if current is not None and current.id == ask.first_outcome_id:
            return ask
    return None


def _after(instant: str, since: str) -> bool:
    a, b = parse_timestamp(instant), parse_timestamp(since)
    return a is None or b is None or a >= b


def ask_owes(ask: Ask, verified: Callable[[str], bool] | None = None) -> bool:
    """The one rule for "a second signature is still owed": the ask is waiting or being signed, or it is signed but
    its signature does not verify (when the caller can check; without a checker a signed ask is taken as given)."""

    if ask.state in OWED:
        return True
    if ask.state != ASK_SIGNED or verified is None:
        return False
    return not (ask.second_outcome_id and verified(ask.second_outcome_id))


def held_approval_ids(
    store: Any, review_id: str, revision_id: str, verified: Callable[[str], bool] | None = None
) -> frozenset[str]:
    """Outcome ids that count for nothing yet while a second signature is owed: the first reviewer's approval, and
    whatever the asked person recorded some way other than signing (an unsigned or failed verdict of theirs)."""

    held: set[str] = set()
    for ask in list_asks(store, review_id):
        if ask.revision_id != revision_id or not ask_owes(ask, verified):
            continue
        current = _current_signing_outcome(store, review_id, ask.first_reviewer_id, revision_id)
        if current is None or current.id != ask.first_outcome_id:
            continue
        held.add(ask.first_outcome_id)
        theirs = _current_signing_outcome(store, review_id, ask.second_reviewer_id, revision_id)
        if theirs is not None and _after(theirs.created_at, ask.requested_at):
            # Only what the asked person recorded since being asked is held; a real decision of theirs from before
            # keeps counting. (A verdict from a failed signing is void: it is not read at all.)
            held.add(theirs.id)
    return frozenset(held)


# --------------------------------------------------------------------------- #
# the Review API's side
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SecondDeps:
    """What the routes need from the Review API (sessions there are already repository-scoped)."""

    store: Any
    authority: SecondReviewerAuthority
    session: Callable[[str], ReviewSession]
    mutable_session: Callable[[str], ReviewSession]
    latest: Callable[[ReviewSession], ReviewRevision]
    decision_parts: Callable[[ReviewSession, ReviewRevision], Sequence[DecisionPart]]
    #: The record of one stored verdict when it is signed AND verifies: ``{record_id, issued_at}``; else ``None``.
    verified_record: Callable[[ReviewSession, str], Mapping[str, Any] | None]
    #: Finishes the Review once both people agreed (it stayed open while the second signature was asked for).
    finish: Callable[[ReviewSession, str], Any] | None = None
    #: Publishes one must-fix note on the Review for a disagreement: ``(session, revision, path, text) -> note id``.
    add_note: Callable[[ReviewSession, ReviewRevision, str, str], str] | None = None


def _person(deps: SecondDeps, session: ReviewSession, principal_id: str) -> dict[str, str] | None:
    if not principal_id:
        return None
    who = deps.authority.person(session, principal_id)
    return {"id": principal_id, "name": who.name or who.handle or principal_id, "handle": who.handle}


def _outcome(store: Any, review_id: str, outcome_id: str) -> ReviewOutcome | None:
    with viewer_lens.unfiltered():
        return next((row for row in store.list_outcomes(review_id) if row.id == outcome_id), None)


def _current_signing_outcome(store: Any, review_id: str, reviewer_id: str, revision_id: str) -> ReviewOutcome | None:
    """The reviewer's newest decision on one revision (a comment is not one)."""

    from lemoncrow.pro.capabilities.review.record_retraction import withheld_outcome_ids

    # A retracted verdict (SPEC 4.18) is not the person's decision any more: an ask made for it no longer stands.
    # The decision of who is asked, and which approvals are held, needs the first reviewer's verdict itself: it reads
    # the full rows even for a viewer the lens keeps from them (what she is shown is decided by the callers).
    with viewer_lens.unfiltered():
        withheld = withheld_outcome_ids(store, review_id)
        for row in store.list_outcomes(review_id, reviewer_id=reviewer_id):  # newest first
            if row.revision_id == revision_id and row.outcome in SIGNING_OUTCOMES and row.id not in withheld:
                return row
    return None


def prospective_plan(
    deps: SecondDeps, session: ReviewSession, revision: ReviewRevision, *, first_id: str
) -> SecondPlan:
    parts = tuple(deps.decision_parts(session, revision))
    owners = deps.authority.owners(session, revision, sorted({part.path for part in parts}))
    return plan_second_reviewer(parts, owners, excluded_ids=(*deps.authority.barred_ids(session), first_id))


def on_verdict(deps: SecondDeps, session: ReviewSession, outcome: ReviewOutcome) -> dict[str, Any]:
    """A person's verdict was stored. Ask the second reviewer only when it is an approval (D1).

    Idempotent. Only the closer's own rules close an ask: a newer revision, or the same first reviewer deciding
    differently. Nobody else's verdict (a comment from a third person, say) touches it. Signs the first
    reviewer's approval when an ask is made, so "1 of 2" is a signature that exists.
    """

    revision = deps.latest(session)
    store = deps.store
    # The asked person's own verdict on this revision is handled by sign(), never by this hook.
    if any(
        ask.second_reviewer_id == outcome.reviewer_id and ask.state in OWED and ask.revision_id == revision.id
        for ask in list_asks(store, session.id)
    ):
        return {"status": "second_reviewer_verdict"}
    decides = outcome.outcome in SIGNING_OUTCOMES
    withdraw_asks(
        store,
        session.id,
        revision_id=revision.id,
        first_reviewer_id=outcome.reviewer_id if decides else "",
        keep_first_outcome_id=outcome.id,
    )
    if not (outcome.outcome == "lgtm" and outcome.revision_id == revision.id):
        return {"status": "not_asked"}
    if deps.authority.signing_unavailable(session):
        return {"status": "unavailable"}
    plan = prospective_plan(deps, session, revision, first_id=outcome.reviewer_id)
    if not plan.required or plan.reviewer is None:
        return {"status": "not_required"}
    ask = request_ask(
        store,
        review_id=session.id,
        revision_id=revision.id,
        first_outcome_id=outcome.id,
        first_reviewer_id=outcome.reviewer_id,
        second_reviewer_id=plan.reviewer.principal_id,
        area=plan.area,
        unit_keys=[part.unit_key for part in plan.parts],
    )
    signed = dict(deps.authority.sign(session.id, outcome.id, {}))
    return {"status": "asked", "ask": ask.relationship.id, "first_signature": signed.get("status", "")}


def owes_signature(deps: SecondDeps, session: ReviewSession) -> bool:
    """Whether a second signature is still owed on the latest revision: asked, and not yet given and verified.

    The one answer both the finish gate and the status share, so "waiting" is never reported without an ask."""

    ask = effective_ask(deps.store, session.id, deps.latest(session).id)
    return ask is not None and ask_owes(ask, lambda outcome_id: deps.verified_record(session, outcome_id) is not None)


def hidden_authors(deps: SecondDeps, session: ReviewSession) -> frozenset[str]:
    """Whose notes the viewer may not read yet: the first reviewer's, for a second reviewer who has not answered.

    Decided once per request: when the request's lens already says, that is the answer."""

    if viewer_lens.known(session.id):
        return viewer_lens.hidden_in(session.id)
    if not list_asks(deps.store, session.id):  # the common read: nobody was ever asked, so one query and done
        return frozenset()
    viewer = deps.authority.viewer_id()
    revision = deps.latest(session)
    ask = effective_ask(deps.store, session.id, revision.id)
    if ask is None or ask.state not in OWED or ask.second_reviewer_id != viewer:
        return frozenset()
    answered_all = ask_answered(ask, _marks(deps.store, session.id, viewer))
    return frozenset() if answered_all else frozenset({ask.first_reviewer_id})


def _marks(store: Any, review_id: str, reviewer_id: str) -> dict[str, str]:
    return {mark.unit_key: str(mark.state) for mark in store.list_marks(review_id, reviewer_id=reviewer_id)}


def build_status(deps: SecondDeps, review_id: str) -> dict[str, Any]:
    """The second-signature status of a Review's latest revision, as the *viewer* may see it.

    This is the only function that lets the first reviewer's answers reach a second reviewer, and it does so
    only after the second reviewer has answered every part they were asked.
    """

    session = deps.session(review_id)
    store = deps.store
    viewer = deps.authority.viewer_id()
    revision = deps.latest(session)
    ask = effective_ask(store, session.id, revision.id)  # read-only: a GET never changes what is stored
    first_id = ask.first_reviewer_id if ask is not None else viewer
    first_outcome = (
        _outcome(store, session.id, ask.first_outcome_id)
        if ask is not None
        else _current_signing_outcome(store, session.id, viewer, revision.id)
    )
    if ask is not None:
        plan = SecondPlan(
            required=True,
            reviewer=OwnerRef(ask.second_reviewer_id),
            area=ask.area,
            parts=tuple(part for part in deps.decision_parts(session, revision) if part.unit_key in ask.unit_keys),
        )
    else:
        plan = prospective_plan(deps, session, revision, first_id=first_id)
        reason = deps.authority.signing_unavailable(session)
        if plan.required and reason:
            plan = SecondPlan(required=False, unavailable=reason)
    second_outcome = (
        _outcome(store, session.id, ask.second_outcome_id) if ask is not None and ask.second_outcome_id else None
    )
    second_record = deps.verified_record(session, second_outcome.id) if second_outcome is not None else None
    state = second_state(
        plan,
        first_outcome=first_outcome.outcome if first_outcome is not None else None,
        # A signature counts when its record exists and verifies, and only then is it agreed or disagreed.
        second_outcome=second_outcome.outcome if second_outcome is not None and second_record is not None else None,
    )
    role = (
        "second"
        if ask is not None and viewer == ask.second_reviewer_id
        else ("first" if viewer == first_id else "other")
    )

    first_record = deps.verified_record(session, first_outcome.id) if first_outcome is not None else None
    have, needed = signature_counts(
        first_signed=first_record is not None, second_signed=second_record is not None, required=plan.required
    )
    payload: dict[str, Any] = {
        "state": state,
        "role": role,
        "required": plan.required,
        "unavailable": plan.unavailable,
        "area": plan.area,
        "decision_count": len(plan.parts),
        "revision_id": revision.id,
        "reviewer": _person(deps, session, plan.reviewer.principal_id) if plan.reviewer is not None else None,
        "first_reviewer": _person(deps, session, first_id) if first_outcome is not None or ask is not None else None,
        "first": (
            None
            if first_outcome is None
            else {
                "outcome_id": first_outcome.id,
                "outcome": first_outcome.outcome,
                "signed": first_record is not None,
                "signed_at": str(first_record.get("issued_at") or "") if first_record else "",
                "record_id": str(first_record.get("record_id") or "") if first_record else "",
            }
        ),
        "second": (
            None
            if second_outcome is None
            else {
                "outcome_id": second_outcome.id,
                "outcome": second_outcome.outcome,
                "signed": second_record is not None,
                "signed_at": str(second_record.get("issued_at") or "") if second_record else "",
                "record_id": str(second_record.get("record_id") or "") if second_record else "",
                "note": second_outcome.summary,
                # Her note reached the author's note set (a published must-fix note), or only her verdict holds it.
                "note_sent": (
                    {"count": 1, "to": "the author"}
                    if ask is not None
                    and ask.note_annotation_id
                    and any(item.id == ask.note_annotation_id for item in store.list_annotations(session.id))
                    else None
                ),
                # What she answered "Needs a change" to, named once she has signed (her signature is no secret).
                "needs_change": [
                    part.label
                    for part in plan.parts
                    if _marks(store, session.id, second_outcome.reviewer_id).get(part.unit_key) == "needs_changes"
                ],
            }
        ),
        "signatures": {"have": have, "needed": needed},
        "verdict": settled_verdict(state, first_outcome.outcome if first_outcome is not None else None),
    }
    if role != "second" or ask is None:
        return payload

    mine = _marks(store, session.id, viewer)
    done = answered(plan, mine)
    payload.update(
        {
            "parts": [
                {
                    "unit_key": part.unit_key,
                    "path": part.path,
                    "label": part.label,
                    "start_line": part.start_line,
                    "end_line": part.end_line,
                    "state": mine.get(part.unit_key, "unreviewed"),
                }
                for part in plan.parts
            ],
            "answered": done,
            "agrees": agrees_with(plan, mine),
            "first_answers_hidden": not first_answers_visible(viewer_is_second=True, second_answered=done),
        }
    )
    if not done:
        # P7-1 F: until she has answered she is told nothing of the first reviewer's verdict: not its kind, not that
        # its record is signed, and not a count of signatures ("1 of 2" says the first one is in).
        payload["first"] = None
        payload["signatures"] = None
    if first_answers_visible(viewer_is_second=True, second_answered=done):
        theirs = _marks(store, session.id, ask.first_reviewer_id)
        notes: dict[str, list[str]] = {}
        for note in store.list_annotations(session.id):
            if note.source == "human" and note.created_by == ask.first_reviewer_id:
                notes.setdefault(note.anchor.path, []).append(note.body)
        payload["first_answers"] = [
            {
                "unit_key": part.unit_key,
                "state": theirs.get(part.unit_key, "unreviewed"),
                "notes": notes.get(part.path, []),
            }
            for part in plan.parts
        ]
    return payload


def _utc_clock(instant: str) -> str:
    from lemoncrow.pro.capabilities.review.record_details import utc_text

    return utc_text(instant)


def record_second_signature(
    deps: SecondDeps, session: ReviewSession, decision: Mapping[str, Any]
) -> dict[str, Any] | None:
    """What a signed record says about the second signature (Details row and the Verify line's tail).

    ``None`` when this is not a person's approval or rejection. A second signature is named only when its own
    signed record exists and verifies; otherwise the row says it is waited for. The wire is
    ``{state, value, reviewer, signed_at, outcome}``.
    """

    if decision.get("kind") != "human_outcome":
        return None
    store = deps.store
    of = second_signature_detail(decision)
    reference = str(decision.get("reference") or "")
    if of:
        first = _outcome(store, session.id, of)
        who = _person(deps, session, first.reviewer_id) if first is not None else None
        name = who["name"] if who else "the first reviewer"
        return {
            "state": "is_second",
            "value": f"co-signs the approval by {name}",
            "reviewer": None,
            "signed_at": "",
            "outcome": "",
        }
    ask = next((item for item in list_asks(store, session.id) if item.first_outcome_id == reference), None)
    if ask is None:
        if decision.get("outcome") == "changes_requested":
            return {
                "state": "not_needed",
                "value": "Not needed · changes were requested",
                "reviewer": None,
                "signed_at": "",
                "outcome": "",
            }
        revision = deps.latest(session)
        outcome = _outcome(store, session.id, reference)
        if outcome is None or outcome.revision_id != revision.id:
            return None
        if deps.authority.signing_unavailable(session):
            return None
        plan = prospective_plan(deps, session, revision, first_id=outcome.reviewer_id)
        if plan.required or plan.unavailable:
            return None
        return {
            "state": "not_required",
            "value": "Not required by this repo's policy",
            "reviewer": None,
            "signed_at": "",
            "outcome": "",
        }
    who = _person(deps, session, ask.second_reviewer_id)
    name = who["name"] if who else ask.second_reviewer_id
    handle = f" · @{who['handle']}" if who and who["handle"] else ""
    second = _outcome(store, session.id, ask.second_outcome_id) if ask.second_outcome_id else None
    verified = deps.verified_record(session, second.id) if second is not None else None
    if second is None or verified is None:
        return {"state": "waiting", "value": f"Waiting · {name}", "reviewer": who, "signed_at": "", "outcome": ""}
    stamp = _utc_clock(str(verified.get("issued_at") or ""))
    word = "agrees" if second.outcome == "lgtm" else "needs a change"
    return {
        "state": "agrees" if second.outcome == "lgtm" else "disagrees",
        "value": f"{name}{handle} · {stamp} · {word}",
        "reviewer": who,
        "signed_at": str(verified.get("issued_at") or ""),
        "outcome": second.outcome,
    }


class SecondReviewerRefused(Exception):
    """A refusal the route turns into an HTTP error: ``(status, detail)``."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def sign(deps: SecondDeps, review_id: str, *, expected_revision_id: str, note: str) -> dict[str, Any]:
    """The second reviewer signs. The verdict is derived from their own answers, never taken from the client.

    The signature is given only when it exists: the verdict is recorded, the organization signs it, and that record
    verifies. Otherwise the ask goes back to waiting and nothing is claimed (503). One request signs at a time."""

    session = deps.mutable_session(review_id)
    store = deps.store
    viewer = deps.authority.viewer_id()
    revision = deps.latest(session)
    if expected_revision_id != revision.id:
        raise SecondReviewerRefused(409, "the review moved to a newer revision; reload before signing")
    ask = next(
        (
            item
            for item in list_asks(store, session.id)
            if item.revision_id == revision.id and item.second_reviewer_id == viewer
        ),
        None,
    )
    if ask is None:
        raise SecondReviewerRefused(403, "nobody asked you for a second signature on this revision")
    if ask.state == ASK_SIGNED:
        raise SecondReviewerRefused(409, "this second signature is already given")
    if ask.state not in OWED:
        raise SecondReviewerRefused(409, "this second signature is no longer asked")
    current = _current_signing_outcome(store, session.id, ask.first_reviewer_id, revision.id)
    if current is None or current.id != ask.first_outcome_id:
        raise SecondReviewerRefused(409, "the first reviewer's approval changed; there is nothing to co-sign")
    parts = tuple(part for part in deps.decision_parts(session, revision) if part.unit_key in ask.unit_keys)
    plan = SecondPlan(required=True, reviewer=OwnerRef(viewer), area=ask.area, parts=parts)
    mine = _marks(store, session.id, viewer)
    if not answered(plan, mine):
        raise SecondReviewerRefused(409, "answer every part first")
    verdict = "lgtm" if agrees_with(plan, mine) else "changes_requested"
    text = note.strip()[:16000]
    if verdict == "changes_requested" and not text:
        raise SecondReviewerRefused(422, "say what needs a change")
    unavailable = deps.authority.signing_unavailable(session)
    if unavailable:
        raise SecondReviewerRefused(503, f"your signature cannot be given now: {unavailable}")
    claimed = claim_ask(store, ask)
    if claimed is None:
        raise SecondReviewerRefused(409, "someone is signing this already; read it again")
    finished = False
    own_claim = _since(claimed)
    recorded_id = ""
    word_before = store.latest_outcome(session.id, viewer)
    try:
        recorded = store.record_outcome(
            session.id,
            revision.id,
            viewer,
            outcome=verdict,
            summary=text,
            expected_current_revision_id=revision.id,
            activity_detail={DETAIL_SECOND_OF: ask.first_outcome_id},
        )
        # The store hands back her existing row when her latest word already is this verdict: nothing new was
        # recorded, so a failure has nothing of this attempt to set aside.
        recorded_id = "" if word_before is not None and recorded.id == word_before.id else recorded.id
        result = dict(deps.authority.sign(session.id, recorded.id, {DETAIL_SECOND_OF: ask.first_outcome_id}))
        if (
            result.get("status") not in {"recorded", "already_recorded"}
            or deps.verified_record(session, recorded.id) is None
        ):
            raise SecondReviewerRefused(503, "your signature could not be recorded; nothing was given. Try again.")
        if complete_ask(store, claimed, second_outcome_id=recorded.id) is None:
            raise SecondReviewerRefused(409, "this second signature changed while you signed; read it again")
        finished = True
    finally:
        if not finished:
            latest = next(
                (item for item in list_asks(store, session.id) if item.relationship.id == claimed.relationship.id),
                claimed,
            )
            # Give the ask back only if the claim is still this request's own: a slow signer that was overtaken
            # must not release the takeover's claim.
            if latest.state == ASK_SIGNING and _since(latest) == own_claim:
                if release_ask(store, latest) is not None and recorded_id:
                    _void(store, session, recorded_id)
    if finished and verdict == "changes_requested":
        _publish_disagreement(deps, session, revision, claimed, parts, mine, text)
    status = build_status(deps, review_id)
    if (
        verdict == "lgtm"
        and status["state"] == "agrees"
        and status["signatures"]["have"] == status["signatures"]["needed"]
    ):
        if deps.finish is not None:
            try:
                deps.finish(session, revision.id)
            except Exception:  # the signatures stand; the next person finishes it
                logging.getLogger(__name__).warning(
                    "review %s agreed by both but not finished", session.id, exc_info=True
                )
    return status


def _publish_disagreement(
    deps: SecondDeps,
    session: ReviewSession,
    revision: ReviewRevision,
    claimed: Ask,
    parts: Sequence[DecisionPart],
    mine: Mapping[str, str],
    text: str,
) -> None:
    """R13 / TEAM-3: the second reviewer's note joins the first reviewer's as a must-fix note, so both reach the author.

    The note is also stored on her signed verdict (that is what Done reads), so a failure here loses nothing but the
    copy in the author's note set: it is logged and Done then says the note is saved, not sent."""

    if deps.add_note is None or not parts:
        return
    path = next((part.path for part in parts if mine.get(part.unit_key) == "needs_changes"), parts[0].path)
    try:
        note_id = deps.add_note(session, revision, path, text)
        fresh = next(
            (item for item in list_asks(deps.store, session.id) if item.relationship.id == claimed.relationship.id),
            None,
        )
        if note_id and fresh is not None:
            _cas(deps.store, fresh, fresh.state, note_annotation_id=note_id)
    except Exception:
        logging.getLogger(__name__).warning(
            "could not publish the second reviewer's note on %s", session.id, exc_info=True
        )


def second_signature_detail(decision: Mapping[str, Any]) -> str:
    """The approval a signed decision is the second signature of (``""`` when it is not one)."""

    detail = decision.get("detail")
    return str(detail.get(DETAIL_SECOND_OF) or "") if isinstance(detail, Mapping) else ""


def with_second_detail(decision: Any, detail: Mapping[str, str]) -> Any:
    return replace(decision, detail={**decision.detail, **detail}) if detail else decision


def register_second_reviewer_routes(router: APIRouter, deps: SecondDeps) -> None:
    """``GET /reviews/{id}/second-reviewer`` and ``POST /reviews/{id}/second-reviewer/sign``."""

    from starlette.concurrency import run_in_threadpool

    from lemoncrow.pro.capabilities.review.api import _json_object

    @router.get("/reviews/{review_id}/second-reviewer")
    async def _get_second_reviewer(review_id: str) -> dict[str, Any]:
        return await run_in_threadpool(build_status, deps, review_id)

    @router.post("/reviews/{review_id}/second-reviewer/sign")
    async def _post_second_reviewer_sign(review_id: str, request: Request) -> dict[str, Any]:
        body = await _json_object(request)
        expected = body.get("expected_revision_id")
        note = body.get("note", "")
        if not isinstance(expected, str) or not expected.strip() or not isinstance(note, str):
            raise HTTPException(status_code=422, detail="name the revision you are signing and give the note as text")
        try:
            return await run_in_threadpool(
                lambda: sign(deps, review_id, expected_revision_id=expected.strip(), note=note)
            )
        except SecondReviewerRefused as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


__all__ = [
    "ASK_KIND",
    "DETAIL_SECOND_OF",
    "Ask",
    "SecondDeps",
    "SecondReviewerAuthority",
    "SecondReviewerRefused",
    "ask_answered",
    "build_status",
    "claim_ask",
    "complete_ask",
    "effective_ask",
    "held_approval_ids",
    "hidden_authors",
    "hidden_by_review",
    "list_asks",
    "on_verdict",
    "owes_signature",
    "record_second_signature",
    "register_second_reviewer_routes",
    "release_ask",
    "request_ask",
    "second_signature_detail",
    "sign",
    "waiting_asks_by_review",
    "with_second_detail",
    "withdraw_asks",
    "withdraw_unstanding_asks",
]
