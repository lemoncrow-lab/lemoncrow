"""Presence, claim and handoff (SPEC 4.16, P7-2).

Hosted only (SPEC 7.1 P0-2). A composition that knows people passes a :class:`TeamAuthority` to the Review API;
without one (local, the default) none of the routes below exist and no payload carries a team field.

What is stored, all as ``review_relationships`` rows of kind ``team_presence`` (no table, no migration), and what
closes each:

* ``reviewing`` (one row per person per Review): written by that person's heartbeat. It means "in it now" for
  :data:`PRESENCE_TTL_SECONDS` after the last beat; after that every reader ignores it, and the next write on the
  Review deletes rows older than :data:`PRESENCE_KEEP_SECONDS`.
* ``claim`` (one row per Review): "I'll take this". Live while ``taken`` and the Review is open and its holder has
  not recorded a verdict since. Closed by a handoff, by the holder's verdict or the Review finishing (read, never
  written), and its closed row is deleted after :data:`CLOSED_KEEP_SECONDS`.
* ``handoff`` (one row per Review): live for :data:`HANDOFF_TTL_SECONDS` or until someone claims the Review or
  hands it on. An away reassignment is only a line on it (``away_until``): it is shown while that instant is in the
  future, and the person's own away record (the authority's) is what ends it.

All rows are children of the Review and go with it.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Request

from lemoncrow.pro.capabilities.review import viewer_lens
from lemoncrow.pro.capabilities.review.freshness import parse_timestamp
from lemoncrow.pro.capabilities.review.second_reviewer import OwnerRef
from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship, ReviewSession
from lemoncrow.pro.capabilities.review.store import utc_now

TEAM_KIND = "team_presence"
REL_REVIEWING = "reviewing"
REL_CLAIM = "claim"
REL_HANDOFF = "handoff"
CLAIM_KEY = "claim"
HANDOFF_KEY = "handoff"
#: A heartbeat is sent about every minute; three missed beats and the person is no longer "in it".
PRESENCE_TTL_SECONDS = 180
PRESENCE_KEEP_SECONDS = 24 * 3600
HANDOFF_TTL_SECONDS = 14 * 24 * 3600
CLOSED_KEEP_SECONDS = 30 * 24 * 3600
NOTE_MAX = 300

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Away:
    """A person's own out-of-office: until when, and who takes their reviews meanwhile."""

    until: str
    reassign_to: str


@dataclass(frozen=True, slots=True)
class RequestMove:
    """What a handoff did to the review request (P9-3).

    ``failure`` is the reason in words when it could not be moved (``code`` is the short fixed code for the audit
    trail); ``record`` says what was moved, so a take-back can reverse exactly that and nothing else: ``kind`` is
    ``delegated`` (the giver's request went to the target), ``cancelled`` (the target was already asked, so only the
    giver's was cancelled) or ``created`` (the giver had none of their own; the target got one), with the ids of the
    giver's request (``giver_request``) and of the target's (``target_request``) where there is one. Empty when
    nothing was moved.
    """

    failure: str | None = None
    code: str = ""
    record: Mapping[str, str] = field(default_factory=dict)


class TeamAuthority(Protocol):
    """What only a composition that knows its people can say. Hosted implements it; local never does."""

    def viewer_id(self) -> str:
        """The authenticated principal of this request (fail closed: raise when none is bound)."""

    def person(self, session: ReviewSession, principal_id: str) -> OwnerRef:
        """How to name one principal."""

    def teammates(self, session: ReviewSession) -> Sequence[OwnerRef]:
        """The people this Review may be handed to (never the viewer, the author or an integration)."""

    def away(self, principal_id: str) -> Away | None:
        """The person's own away record while it is in force, else ``None``."""

    def tell(self, session: ReviewSession, principal_ids: Sequence[str], reason: str, *, assign: bool) -> Sequence[str]:
        """Put a reason in front of these people (a manual attention reason); ``assign`` also makes them reviewers.

        Never brings back a participant who was removed. Returns the ids that were reached."""

    def untell(self, session: ReviewSession, principal_ids: Sequence[str]) -> None:
        """Take back the manual reason ``tell`` put in front of these people."""

    def move_request(self, session: ReviewSession, giver: str, target: str) -> RequestMove:
        """Move the giver's review request (or the team's they answer to) to ``target``: the target is asked in the
        Inbox and Home sense and the giver is no longer asked. Where nothing can be moved it does nothing; a request
        that could not be moved says why in the result."""

    def restore_request(self, session: ReviewSession, record: Mapping[str, str]) -> str | None:
        """Reverse exactly what ``record`` (from :meth:`move_request`) moved, for the giver who takes the review
        back; returns the reason in words when that could not be done, else ``None``."""

    def human(self) -> bool:
        """Whether this request's principal is a person (a service token or an agent is not)."""

    def may_take(self, session: ReviewSession, principal_id: str) -> bool:
        """Whether this person may take the review: not its author and not an integration or agent."""

    def gone(self, session: ReviewSession, principal_id: str) -> bool:
        """Whether this person has been removed from the review."""


# --------------------------------------------------------------------------- #
# rows
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Row:
    relationship: ReviewRelationship
    meta: Mapping[str, Any]

    @property
    def relation(self) -> str:
        return self.relationship.relation

    @property
    def status(self) -> str:
        return self.relationship.status

    @property
    def review_id(self) -> str:
        return self.relationship.review_id

    @property
    def key(self) -> str:
        return self.relationship.external_id


def _parse(row: ReviewRelationship) -> Row:
    try:
        meta = json.loads(row.metadata_json or "{}")
    except ValueError:
        meta = {}
    return Row(row, meta if isinstance(meta, dict) else {})


def rows_of(store: Any, review_id: str) -> tuple[Row, ...]:
    return tuple(_parse(row) for row in store.list_relationships(review_id, kind=TEAM_KIND))


def rows_by_review(store: Any, review_ids: Sequence[str]) -> dict[str, tuple[Row, ...]]:
    """The team rows of these Reviews, by Review (one read for an Inbox page)."""

    wanted = [str(item) for item in review_ids]
    if not wanted:
        return {}
    grouped: dict[str, list[Row]] = {}
    for row in store.list_relationships_for_reviews(TEAM_KIND, wanted):
        grouped.setdefault(row.review_id, []).append(_parse(row))
    return {review_id: tuple(rows) for review_id, rows in grouped.items()}


def _age(instant: object, now: datetime) -> float | None:
    at = parse_timestamp(instant)
    return None if at is None else (now - at).total_seconds()


def _person(who: OwnerRef) -> dict[str, str]:
    return {"id": who.principal_id, "name": who.name or who.handle or who.principal_id}


def _named(meta: Mapping[str, Any], key: str) -> dict[str, str]:
    return {"id": str(meta.get(f"{key}_id") or ""), "name": str(meta.get(f"{key}_name") or "")}


def live_presence(rows: Sequence[Row], now: datetime) -> list[Row]:
    """People in the Review now, longest first."""

    live = [
        row
        for row in rows
        if row.relation == REL_REVIEWING
        and row.status == "active"
        and (age := _age(row.meta.get("seen_at"), now)) is not None
        and -5 <= age <= PRESENCE_TTL_SECONDS
    ]
    return sorted(live, key=lambda row: (str(row.meta.get("started_at") or ""), row.key))


def live_claim(
    rows: Sequence[Row],
    *,
    open_review: bool,
    verdict_since: Callable[[str, str], bool],
    gone: Callable[[str], bool] = lambda _who: False,
) -> Row | None:
    """The standing claim: taken, the Review open, its holder still on it, and not decided since taking it."""

    claim = next((row for row in rows if row.relation == REL_CLAIM and row.status == "taken"), None)
    if claim is None or not open_review:
        return None
    holder = str(claim.meta.get("by_id") or "")
    if gone(holder):
        return None
    return None if verdict_since(holder, str(claim.meta.get("at") or "")) else claim


def live_handoff(
    rows: Sequence[Row], now: datetime, *, open_review: bool, gone: Callable[[str], bool] = lambda _who: False
) -> Row | None:
    """The standing handoff: active, within its days, and neither of its two people taken off the review."""

    row = next((item for item in rows if item.relation == REL_HANDOFF and item.status == "active"), None)
    if row is None or not open_review:
        return None
    if gone(str(row.meta.get("to_id") or "")) or gone(str(row.meta.get("by_id") or "")):
        return None
    age = _age(row.meta.get("at"), now)
    return row if age is not None and age <= HANDOFF_TTL_SECONDS else None


def _away_line(handoff: Row, now: datetime) -> dict[str, Any] | None:
    """The away line, only while the person's away period is still ahead; ``rerouted`` says whether it went to a stand-in."""

    via_id = str(handoff.meta.get("via_id") or "")
    until = parse_timestamp(handoff.meta.get("away_until"))
    if not via_id or until is None or until <= now:
        return None
    return {
        **_named(handoff.meta, "via"),
        "until": until.isoformat(),
        "rerouted": handoff.meta.get("rerouted", True) is not False,
    }


def team_wire(
    rows: Sequence[Row],
    viewer: str,
    *,
    now: datetime,
    open_review: bool,
    verdict_since: Callable[[str, str], bool],
    gone: Callable[[str], bool] = lambda _who: False,
    hidden: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """What a viewer sees of a Review's team state (one shape for the Inbox row and the Review).

    A handoff's note, and who wrote it, are for the two people it is between: anyone else (a second reviewer who
    has not answered yet among them) sees that it was handed over, not what the first reviewer said (SPEC 7.1 P7-1 F).
    """

    # A first reviewer a second reviewer may not read yet is not listed as being in the review (P7-1 F: no sign of his work).
    others = [row for row in live_presence(rows, now) if row.key != viewer and row.key not in hidden]
    claim = live_claim(rows, open_review=open_review, verdict_since=verdict_since, gone=gone)
    # A claim a first reviewer a second reviewer may not read yet holds is not shown to her (no sign of his work).
    if claim is not None and str(claim.meta.get("by_id") or "") in hidden:
        claim = None
    handoff = live_handoff(rows, now, open_review=open_review, gone=gone)
    # A claim made after a handoff supersedes it, and a handoff made after a claim ended that claim.
    if claim is not None and handoff is not None:
        if str(claim.meta.get("at") or "") >= str(handoff.meta.get("at") or ""):
            handoff = None
        else:
            claim = None
    claim_wire = None
    if claim is not None:
        by = _named(claim.meta, "by")
        present = [str(i) for i in claim.meta.get("present_ids") or []]
        names = [str(n) for n in claim.meta.get("present_names") or []]
        acked = claim.meta.get("acked") or {}
        # Who was in it when it was taken: a first reviewer she may not read yet is not named to her either.
        pairs = [(i, n) for i, n in zip(present, names, strict=False) if i not in hidden]
        claim_wire = {
            "by": by,
            "by_me": by["id"] == viewer,
            "at": str(claim.meta.get("at") or ""),
            # Told: their own beat picked the notice up. Waiting: they were in it when it was taken and have not yet.
            "told": [{"id": i, "name": n} for i, n in pairs if i in acked],
            "waiting": [{"id": i, "name": n} for i, n in pairs if i not in acked],
        }
    handoff_wire = None
    if handoff is not None:
        to = _named(handoff.meta, "to")
        by = _named(handoff.meta, "by")
        # The words are for the two people it is between, and not for a target who must not read the giver yet.
        between = viewer in {to["id"], by["id"]} and by["id"] not in hidden
        handoff_wire = {
            "to": to,
            "by": by if between else {"id": "", "name": ""},
            "to_me": to["id"] == viewer,
            "by_me": by["id"] == viewer,
            "note": str(handoff.meta.get("note") or "") if between else "",
            "at": str(handoff.meta.get("at") or ""),
            "away": _away_line(handoff, now),
        }
    return {
        "reviewing": [
            {
                "id": row.key,
                "name": str(row.meta.get("name") or row.key),
                "started_at": str(row.meta.get("started_at") or ""),
            }
            for row in others
        ],
        "claim": claim_wire,
        "handoff": handoff_wire,
    }


def row_wire(
    rows: Sequence[Row],
    viewer: str,
    *,
    now: datetime,
    open_review: bool,
    verdict_since: Callable[[str, str], bool],
    gone: Callable[[str], bool] = lambda _who: False,
    hidden: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """The Inbox row's ``team``: ``None`` when nobody is in it and nothing is claimed or handed off."""

    wire = team_wire(
        rows, viewer, now=now, open_review=open_review, verdict_since=verdict_since, gone=gone, hidden=hidden
    )
    return wire if wire["reviewing"] or wire["claim"] or wire["handoff"] else None


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #


def _write(store: Any, review_id: str, relation: str, key: str, status: str, meta: Mapping[str, Any]) -> Row:
    return _parse(
        store.upsert_relationship(
            ReviewRelationship(
                id="",
                review_id=review_id,
                kind=TEAM_KIND,
                relation=relation,
                external_id=key,
                status=status,
                source="lemoncrow",
                metadata_json=json.dumps(dict(meta), sort_keys=True),
            )
        )
    )


def prune(store: Any, review_id: str, rows: Sequence[Row], now: datetime) -> int:
    """Delete rows nobody reads any more (called by every write on the Review)."""

    gone = 0
    for row in rows:
        if row.relation == REL_REVIEWING:
            age = _age(row.meta.get("seen_at"), now)
            stale = age is None or age > PRESENCE_KEEP_SECONDS
        else:
            age = _age(row.meta.get("closed_at") or row.meta.get("at"), now)
            closed = row.status in {"released", "ended"}
            stale = (
                age is None or (closed and age > CLOSED_KEEP_SECONDS) or age > CLOSED_KEEP_SECONDS + HANDOFF_TTL_SECONDS
            )
        if stale and store.delete_relationship(
            review_id, kind=TEAM_KIND, relation=row.relation, provider="", external_id=row.key
        ):
            gone += 1
    return gone


def heartbeat(store: Any, review_id: str, who: OwnerRef, rows: Sequence[Row], now: datetime) -> None:
    """Record that this person is in the Review now; their ``started_at`` survives beats and resets after a gap."""

    mine = next((row for row in rows if row.relation == REL_REVIEWING and row.key == who.principal_id), None)
    age = _age(mine.meta.get("seen_at"), now) if mine is not None else None
    started = (
        str(mine.meta.get("started_at") or "")
        if mine is not None and age is not None and age <= PRESENCE_TTL_SECONDS
        else ""
    )
    stamp = utc_now()
    _write(
        store,
        review_id,
        REL_REVIEWING,
        who.principal_id,
        "active",
        {"name": _person(who)["name"], "started_at": started or stamp, "seen_at": stamp},
    )


class TeamRefused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class TeamDeps:
    store: Any
    authority: TeamAuthority
    session: Callable[[str], ReviewSession]
    mutable_session: Callable[[str], ReviewSession]
    #: Whose notes this viewer may not read yet (a second reviewer's anti-anchoring); ``None`` where there is none.
    hidden: Callable[[ReviewSession], frozenset[str]] | None = None


def _open(session: ReviewSession) -> bool:
    return str(session.status) == "open"


def _verdict_since(store: Any, review_id: str) -> Callable[[str, str], bool]:
    def since(principal_id: str, instant: str) -> bool:
        if not principal_id or not instant:
            return False
        outcome = store.latest_outcome(review_id, principal_id)
        taken = parse_timestamp(instant)
        decided = None if outcome is None else parse_timestamp(outcome.created_at)
        return taken is not None and decided is not None and decided > taken

    return since


def _wire_of(
    deps: TeamDeps, session: ReviewSession, viewer: str, rows: Sequence[Row], now: datetime, *, decide: bool = False
) -> dict[str, Any]:
    """The team state as ``viewer`` is shown it. ``decide=True`` is the whole state, for code that decides (take, hand
    off): who holds the review and who is in it do not depend on what this viewer may read. Only what is returned to
    her is narrowed (a refusal names no one she may not read: :func:`_named_holder`)."""

    hidden = frozenset() if decide or deps.hidden is None else deps.hidden(session)
    return team_wire(
        rows,
        viewer,
        now=now,
        open_review=_open(session),
        verdict_since=_verdict_since(deps.store, session.id),
        gone=lambda who: deps.authority.gone(session, who),
        hidden=frozenset(hidden),
    )


def _sent_notes(deps: TeamDeps, session: ReviewSession, principal_id: str) -> int | None:
    """Notes this person has sent on the current revision; ``None`` when this viewer may not know yet."""

    if deps.hidden is not None and principal_id in deps.hidden(session):
        return None
    revision = deps.store.latest_revision(session.id)
    if revision is None:
        return 0
    return sum(
        1
        for item in deps.store.list_annotations(session.id)
        if item.created_by == principal_id
        and item.source == "human"
        and item.revision_id == revision.id
        and item.state not in {"obsolete"}
    )


def build_status(deps: TeamDeps, review_id: str) -> dict[str, Any]:
    session = deps.session(review_id)
    viewer = deps.authority.viewer_id()
    now = datetime.now(UTC)
    rows = rows_of(deps.store, session.id)
    wire = _wire_of(deps, session, viewer, rows, now)
    for person in wire["reviewing"]:
        person["sent_notes"] = _sent_notes(deps, session, person["id"])
    wire["viewer"] = viewer
    notices: list[dict[str, Any]] = []
    claim, handoff = wire["claim"], wire["handoff"]
    # A claim's notice is for the people who were in it, until their own beat has picked it up.
    if claim is not None and not claim["by_me"] and any(item["id"] == viewer for item in claim["waiting"]):
        notices.append({"kind": "taken", "by": claim["by"], "at": claim["at"]})
    if handoff is not None and handoff["to_me"]:
        notices.append({"kind": "handed_off", "by": handoff["by"], "note": handoff["note"], "at": handoff["at"]})
    wire["notices"] = notices
    return wire


# One process-wide lock keeps the read-check-write of a claim from interleaving inside one server process. It is not
# what makes taking or handing off a review exclusive across replicas: ``take`` writes the claim with an insert-if-absent
# (no row yet) or a compare-and-set on the row it read (a released, ended or abandoned one), ``hand_off`` writes its row
# the same way, and each reads the other's rows again on a fresh read before writing and once more after (P9-1, P9-3).
# The claim and the handoff are two rows, so no single compare-and-set excludes both; write-then-read does: of a claim
# and a handoff written at the same moment at least one sees the other after its own write and takes its own back.
_CLAIM_LOCK = threading.Lock()


def _acknowledge(deps: TeamDeps, session: ReviewSession, viewer: str, rows: Sequence[Row], now: datetime) -> None:
    """The viewer's beat picks up the notice of a claim made while they were in the review: record that they were told."""

    with _CLAIM_LOCK:
        claim = live_claim(
            rows_of(deps.store, session.id),
            open_review=True,
            verdict_since=_verdict_since(deps.store, session.id),
            gone=lambda who: deps.authority.gone(session, who),
        )
        if claim is None or viewer not in [str(i) for i in claim.meta.get("present_ids") or []]:
            return
        acked = dict(claim.meta.get("acked") or {})
        if viewer in acked:
            return
        acked[viewer] = utc_now()
        deps.store.update_relationship_if_unchanged(
            claim.relationship, status="taken", metadata_json=json.dumps({**claim.meta, "acked": acked}, sort_keys=True)
        )


def beat(deps: TeamDeps, review_id: str) -> dict[str, Any]:
    session = deps.session(review_id)
    viewer = deps.authority.viewer_id()
    now = datetime.now(UTC)
    if not deps.authority.human():
        return build_status(deps, review_id)  # a service token is not a person in the review
    rows = rows_of(deps.store, session.id)
    if not _open(session):
        return build_status(deps, review_id)
    prune(deps.store, session.id, rows, now)
    heartbeat(deps.store, session.id, deps.authority.person(session, viewer), rows, now)
    status = build_status(deps, review_id)  # its notices are read before this beat acknowledges them
    _acknowledge(deps, session, viewer, rows, now)
    after = build_status(deps, review_id)
    after["notices"] = status["notices"]
    return after


def _need_person(deps: TeamDeps) -> None:
    if not deps.authority.human():
        raise TeamRefused(403, "only a person can take or hand off a review")


def _hidden_of(deps: TeamDeps, session: ReviewSession) -> frozenset[str]:
    return frozenset(deps.hidden(session)) if deps.hidden is not None else frozenset()


def _named_holder(deps: TeamDeps, session: ReviewSession, meta: Mapping[str, Any]) -> str:
    """Who holds the claim, as this viewer may be told: not by name when it is someone she may not read yet (a first
    reviewer a second reviewer has not answered). The refusal then says only that someone did."""

    hidden = deps.hidden(session) if deps.hidden is not None else frozenset()
    return "Someone" if str(meta.get("by_id") or "") in hidden else str(meta.get("by_name") or "Someone")


def _handed_to_other(standing: Row | None, viewer: str, hidden: frozenset[str] = frozenset()) -> TeamRefused | None:
    """The refusal for a live handoff the viewer is not one of the two people of, else ``None``."""

    if standing is None:
        return None
    ends = {str(standing.meta.get("to_id") or ""), str(standing.meta.get("by_id") or "")}
    if viewer in ends:
        return None
    named = "" if str(standing.meta.get("to_id") or "") in hidden else str(standing.meta.get("to_name") or "")
    return TeamRefused(409, f"this review was handed to {named or 'someone else'}")


def take(deps: TeamDeps, review_id: str) -> dict[str, Any]:
    """\"I'll take this\": claim the Review for the viewer. The people who are in it are told at their next beat."""

    session = deps.mutable_session(review_id)
    viewer = deps.authority.viewer_id()
    _need_person(deps)
    if not _open(session):
        raise TeamRefused(409, "this review is finished, so there is nothing to take")
    if not deps.authority.may_take(session, viewer):
        raise TeamRefused(403, "the author of a review and an integration cannot take it")
    now = datetime.now(UTC)
    with _CLAIM_LOCK:
        rows = rows_of(deps.store, session.id)
        with viewer_lens.unfiltered():  # a decision reads the whole state; only the refusal's words are narrowed
            status = _wire_of(deps, session, viewer, rows, now, decide=True)
        holder = status["claim"]
        if holder is not None:
            if holder["by_me"]:
                return build_status(deps, review_id)
            held = {"by_id": holder["by"]["id"], "by_name": holder["by"]["name"]}
            raise TeamRefused(409, f"{_named_holder(deps, session, held)} already took this review")
        handoff_row = live_handoff(rows, now, open_review=True, gone=lambda who: deps.authority.gone(session, who))
        refused = _handed_to_other(handoff_row, viewer, _hidden_of(deps, session))
        if refused is not None:
            raise refused
        prune(deps.store, session.id, rows, now)
        me = deps.authority.person(session, viewer)
        present = list(status["reviewing"])
        claim_meta = json.dumps(
            {
                "by_id": viewer,
                "by_name": _person(me)["name"],
                "at": utc_now(),
                # Who was in it. Each is told at their own next beat, which records it (``acked``).
                "present_ids": [item["id"] for item in present],
                "present_names": [item["name"] for item in present],
                "acked": {},
            },
            sort_keys=True,
        )
        # The row as it is after the prune: a released, ended or abandoned claim is taken over by compare-and-set,
        # no claim row at all by insert-if-absent. Either fails when another request got there first.
        fresh = rows_of(deps.store, session.id)

        # The checks above read from before the prune, and another replica may have taken or handed off the review
        # since: a live claim in this read is never the base of a compare-and-set (that would overwrite it), it is the
        # answer, and so is a handoff to someone else.
        def gone(who: str) -> bool:
            return deps.authority.gone(session, who)

        now_held = live_claim(fresh, open_review=True, verdict_since=_verdict_since(deps.store, session.id), gone=gone)
        if now_held is not None:
            if str(now_held.meta.get("by_id")) == viewer:
                return build_status(deps, review_id)
            raise TeamRefused(409, f"{_named_holder(deps, session, now_held.meta)} already took this review")
        refused = _handed_to_other(
            live_handoff(fresh, datetime.now(UTC), open_review=True, gone=gone), viewer, _hidden_of(deps, session)
        )
        if refused is not None:
            raise refused
        existing = next((row for row in fresh if row.relation == REL_CLAIM and row.key == CLAIM_KEY), None)
        if existing is None:
            taken = deps.store.insert_relationship_if_absent(
                ReviewRelationship(
                    id="",
                    review_id=session.id,
                    kind=TEAM_KIND,
                    relation=REL_CLAIM,
                    external_id=CLAIM_KEY,
                    status="taken",
                    source="lemoncrow",
                    metadata_json=claim_meta,
                )
            )
        else:
            taken = deps.store.update_relationship_if_unchanged(
                existing.relationship, status="taken", metadata_json=claim_meta
            )
        if taken is None:
            raise TeamRefused(409, "someone else took this review at the same moment")
        # Write, then read: a handoff to someone else that landed after the fresh read above is seen here, and the
        # claim just written is given back (the handoff's own writer reads our claim after its write too).
        refused = _handed_to_other(
            live_handoff(rows_of(deps.store, session.id), datetime.now(UTC), open_review=True, gone=gone),
            viewer,
            _hidden_of(deps, session),
        )
        if refused is not None:
            # By compare-and-set on a fresh read: a beat that recorded "was told" in the meantime changed the row.
            _release_claim(deps, session, viewer, "conflict")
            raise refused
        moved: str | None = None
        restored = False
        if handoff_row is not None:
            # The handoff's reason has done its job, whether the target or the giver took the review.
            to_id, by_id = str(handoff_row.meta.get("to_id") or ""), str(handoff_row.meta.get("by_id") or "")
            deps.authority.untell(session, [to_id])
            record = handoff_row.meta.get("request_move")
            if viewer == by_id and to_id and to_id != viewer and isinstance(record, dict) and record:
                # The giver took it back: reverse exactly what the handoff moved (a handoff with no record, from
                # before it was kept, moved nothing that can be reversed).
                moved = deps.authority.restore_request(session, record)
                restored = True
    status_wire = build_status(deps, review_id)
    if restored:
        status_wire["request_moved"] = moved is None
        if moved is not None:
            status_wire["request_move_error"] = moved
    return status_wire


def _release_claim(deps: TeamDeps, session: ReviewSession, viewer: str, closed_by: str) -> bool:
    """Give the viewer's standing claim back by compare-and-set; ``False`` when they hold none.

    A beat that records "was told" changes the row too, so a lost compare-and-set reads the row again (a few times).
    """

    for _attempt in range(4):
        holder = next(
            (row for row in rows_of(deps.store, session.id) if row.relation == REL_CLAIM and row.status == "taken"),
            None,
        )
        if holder is None or str(holder.meta.get("by_id")) != viewer:
            return False
        closed = json.dumps({**holder.meta, "closed_at": utc_now(), "closed_by": closed_by}, sort_keys=True)
        if deps.store.update_relationship_if_unchanged(holder.relationship, status="released", metadata_json=closed):
            return True
    raise TeamRefused(409, "this review's claim changed at the same moment; try again")


def release(deps: TeamDeps, review_id: str) -> dict[str, Any]:
    """\"Let go\": the holder gives the claim back."""

    session = deps.mutable_session(review_id)
    viewer = deps.authority.viewer_id()
    _need_person(deps)
    with _CLAIM_LOCK:
        if not _release_claim(deps, session, viewer, "release"):
            raise TeamRefused(409, "you have not taken this review")
    return build_status(deps, review_id)


def _remember_move(deps: TeamDeps, review_id: str, stamp: str, record: Mapping[str, str]) -> None:
    """Keep what the handoff moved on the handoff row itself, so a take-back reverses that and nothing else. Without
    this word (it could not be written) a take-back moves nothing."""

    for _attempt in range(4):
        row = next(
            (r for r in rows_of(deps.store, review_id) if r.relation == REL_HANDOFF and r.key == HANDOFF_KEY), None
        )
        if row is None or row.status != "active" or str(row.meta.get("at") or "") != stamp:
            return  # a newer handoff (or none) stands: its own move is its own record
        words = json.dumps({**row.meta, "request_move": dict(record)}, sort_keys=True)
        if deps.store.update_relationship_if_unchanged(row.relationship, status="active", metadata_json=words):
            return
    _LOG.warning("could not record what a handoff moved on review %s", review_id)


def _withdraw_handoff(
    deps: TeamDeps, review_id: str, written: ReviewRelationship, ours: str, before: Row | None, ended: str
) -> None:
    """Take back the handoff row this request just wrote: restore what stood before it (status and words exactly), or
    end it when nothing did. Compare-and-set on a fresh read, a few times; a row that no longer holds our words is
    someone else's newer word and is left alone."""

    status, words = ("ended", ended) if before is None else (before.status, before.relationship.metadata_json)
    expected = written
    for _attempt in range(4):
        if deps.store.update_relationship_if_unchanged(expected, status=status, metadata_json=words) is not None:
            return
        row = next(
            (r for r in rows_of(deps.store, review_id) if r.relation == REL_HANDOFF and r.key == HANDOFF_KEY), None
        )
        if row is None or row.relationship.metadata_json != ours or row.status != written.status:
            return  # not our words, or a status-only change (a prune ended it) that is not ours to overwrite
        expected = row.relationship


def _hand_off_blocked(
    deps: TeamDeps, session: ReviewSession, viewer: str, rows: Sequence[Row], now: datetime
) -> Row | None:
    """Refuse (409) when someone else holds the review or it is handed to others; else the standing handoff, if any."""

    with viewer_lens.unfiltered():  # decided on the whole state, a hidden holder's claim included
        current = _wire_of(deps, session, viewer, rows, now, decide=True)
    if current["claim"] is not None and not current["claim"]["by_me"]:
        held = {"by_id": current["claim"]["by"]["id"], "by_name": current["claim"]["by"]["name"]}
        raise TeamRefused(409, f"{_named_holder(deps, session, held)} took this review")
    standing = live_handoff(rows, now, open_review=True, gone=lambda who: deps.authority.gone(session, who))
    if standing is not None and current["claim"] is None:
        refused = _handed_to_other(standing, viewer, _hidden_of(deps, session))
        if refused is not None:
            raise refused
    return standing


def hand_off(deps: TeamDeps, review_id: str, *, to: str, note: str) -> dict[str, Any]:
    """Hand the Review to a teammate with a note; an away teammate's reviews go to their stand-in when that person is on it."""

    session = deps.mutable_session(review_id)
    viewer = deps.authority.viewer_id()
    _need_person(deps)
    if not _open(session):
        raise TeamRefused(409, "this review is finished, so there is nothing to hand off")
    note = note.strip()
    if len(note) > NOTE_MAX:
        raise TeamRefused(422, f"keep the note to {NOTE_MAX} characters")
    mates = {who.principal_id: who for who in deps.authority.teammates(session)}
    target = mates.get(to)
    if target is None or to == viewer:
        raise TeamRefused(422, "pick a teammate who is on this review's team")
    now = datetime.now(UTC)
    with _CLAIM_LOCK:
        rows = rows_of(deps.store, session.id)
        _hand_off_blocked(deps, session, viewer, rows, now)
        away = deps.authority.away(to)
        via: OwnerRef | None = None
        away_until = ""
        rerouted = True
        if away is not None:
            until = parse_timestamp(away.until)
            if until is not None and until > now:
                via, away_until = target, until.isoformat()
                stand_in = mates.get(away.reassign_to)
                if stand_in is not None:
                    target = stand_in
                else:
                    rerouted = False  # their stand-in is not on this review: it goes to them as asked
        prune(deps.store, session.id, rows, now)
        me = deps.authority.person(session, viewer)
        stamp = utc_now()
        # The rows as they are after the prune: another replica may have taken the review or handed it on since the
        # first read. A claim of someone else in this read is the answer; the handoff row is then written by
        # insert-if-absent (none yet) or compare-and-set on the row read, never over a row nobody read.
        fresh = rows_of(deps.store, session.id)
        standing = _hand_off_blocked(deps, session, viewer, fresh, datetime.now(UTC))
        previous = str(standing.meta.get("to_id") or "") if standing is not None else ""
        meta: dict[str, Any] = {
            "by_id": viewer,
            "by_name": _person(me)["name"],
            "to_id": target.principal_id,
            "to_name": _person(target)["name"],
            "note": note,
            "at": stamp,
        }
        if via is not None:
            meta.update(
                {
                    "via_id": via.principal_id,
                    "via_name": _person(via)["name"],
                    "away_until": away_until,
                    "rerouted": rerouted,
                }
            )
        existing = next((row for row in fresh if row.relation == REL_HANDOFF and row.key == HANDOFF_KEY), None)
        meta_json = json.dumps(meta, sort_keys=True)
        if existing is None:
            written = deps.store.insert_relationship_if_absent(
                ReviewRelationship(
                    id="",
                    review_id=session.id,
                    kind=TEAM_KIND,
                    relation=REL_HANDOFF,
                    external_id=HANDOFF_KEY,
                    status="active",
                    source="lemoncrow",
                    metadata_json=meta_json,
                )
            )
        else:
            written = deps.store.update_relationship_if_unchanged(
                existing.relationship, status="active", metadata_json=meta_json
            )
        if written is None:
            raise TeamRefused(409, "someone else handed this review on at the same moment")
        # Write, then read: a claim by someone else that landed after the fresh read above is seen here, and the
        # handoff just written is withdrawn (the claim's own writer reads our handoff after its write too).
        try:
            _hand_off_blocked(deps, session, viewer, rows_of(deps.store, session.id), datetime.now(UTC))
        except TeamRefused:
            # A handoff that stood before this one stands again; one that did not exist is ended.
            _withdraw_handoff(
                deps,
                session.id,
                written,
                meta_json,
                existing,
                json.dumps({**meta, "closed_at": utc_now(), "closed_by": "conflict"}, sort_keys=True),
            )
            raise
        # Handing it on lets go of the viewer's own claim.
        _release_claim(deps, session, viewer, "handoff")
        if previous and previous != target.principal_id:
            deps.authority.untell(session, [previous])
    # The reason is stored and cannot be hidden later: a target who must not read the giver yet gets no words in it.
    hides = _target_hides_giver(deps, session, giver=viewer, target=target.principal_id)
    reason = f"{_person(me)['name']} handed this review to you" + (f": {note}" if note and not hides else "")
    deps.authority.tell(session, [target.principal_id], reason, assign=True)
    moved = deps.authority.move_request(session, viewer, target.principal_id)
    if moved.record:
        _remember_move(deps, session.id, stamp, moved.record)
    result = build_status(deps, review_id)
    result["request_moved"] = moved.failure is None
    if moved.failure is not None:
        result["request_move_error"] = moved.failure
    return result


def _target_hides_giver(deps: TeamDeps, session: ReviewSession, *, giver: str, target: str) -> bool:
    """Whether the target is a second reviewer, still owed, from whom the giver's words are held back (P7-1 F)."""

    from lemoncrow.pro.capabilities.review.second_reviewer_api import OWED, list_asks

    return any(
        ask.state in OWED and ask.second_reviewer_id == target and ask.first_reviewer_id == giver
        for ask in list_asks(deps.store, session.id)
    )


def clear_review(store: Any, review_id: str) -> int:
    """The review is over: every presence, claim and handoff row of it goes (called when it is finished)."""

    gone = 0
    for row in rows_of(store, review_id):
        if store.delete_relationship(
            review_id, kind=TEAM_KIND, relation=row.relation, provider="", external_id=row.key
        ):
            gone += 1
    return gone


def end_for_person(store: Any, review_id: str, principal_id: str, untell: Callable[[str], None] | None = None) -> int:
    """SCIM deprovisioning: this person's presence goes, a claim they hold is released and a handoff by or to them ends."""

    changed = 0
    stamp = utc_now()
    for row in rows_of(store, review_id):
        if row.relation == REL_REVIEWING and row.key == principal_id:
            changed += int(
                store.delete_relationship(
                    review_id, kind=TEAM_KIND, relation=row.relation, provider="", external_id=row.key
                )
            )
        elif row.relation == REL_CLAIM and row.status == "taken" and row.meta.get("by_id") == principal_id:
            _write(
                store,
                review_id,
                REL_CLAIM,
                CLAIM_KEY,
                "released",
                {**row.meta, "closed_at": stamp, "closed_by": "deprovision"},
            )
            changed += 1
        elif (
            row.relation == REL_HANDOFF
            and row.status == "active"
            and principal_id in {row.meta.get("by_id"), row.meta.get("to_id")}
        ):
            _write(
                store,
                review_id,
                REL_HANDOFF,
                HANDOFF_KEY,
                "ended",
                {**row.meta, "closed_at": stamp, "closed_by": "deprovision"},
            )
            if untell is not None and row.meta.get("to_id"):
                untell(str(row.meta["to_id"]))  # the target keeps no reason about a handoff that is over
            changed += 1
    return changed


def teammates_wire(deps: TeamDeps, review_id: str) -> dict[str, Any]:
    session = deps.session(review_id)
    now = datetime.now(UTC)
    mates = {who.principal_id: who for who in deps.authority.teammates(session)}
    people: list[dict[str, Any]] = []
    for who in mates.values():
        item: dict[str, Any] = {**_person(who), "away": None}
        away = deps.authority.away(who.principal_id)
        until = None if away is None else parse_timestamp(away.until)
        if away is not None and until is not None and until > now:
            stand_in = mates.get(away.reassign_to)
            item["away"] = {"until": until.isoformat(), "reassign_to": None if stand_in is None else _person(stand_in)}
        people.append(item)
    return {"teammates": people}


def register_team_routes(router: APIRouter, deps: TeamDeps) -> None:
    """``/reviews/{id}/team`` (read) and its ``presence``, ``claim``, ``release``, ``handoff`` and ``teammates``."""

    from starlette.concurrency import run_in_threadpool

    from lemoncrow.pro.capabilities.review.api import _json_object

    def refused(exc: TeamRefused) -> HTTPException:
        return HTTPException(status_code=exc.status, detail=exc.detail)

    def decide(fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
        """A write decides on the whole Review whatever lens the request is in; only its answer is narrowed (by the
        hidden people the deps name). One rule for presence, claim, release and hand-off."""

        with viewer_lens.unfiltered():
            return fn(*args, **kwargs)

    @router.get("/reviews/{review_id}/team")
    async def _get_team(review_id: str) -> dict[str, Any]:
        return await run_in_threadpool(build_status, deps, review_id)

    @router.get("/reviews/{review_id}/team/teammates")
    async def _get_teammates(review_id: str) -> dict[str, Any]:
        return await run_in_threadpool(teammates_wire, deps, review_id)

    @router.post("/reviews/{review_id}/team/presence")
    async def _post_presence(review_id: str) -> dict[str, Any]:
        return await run_in_threadpool(decide, beat, deps, review_id)

    @router.post("/reviews/{review_id}/team/claim")
    async def _post_claim(review_id: str) -> dict[str, Any]:
        try:
            return await run_in_threadpool(decide, take, deps, review_id)
        except TeamRefused as exc:
            raise refused(exc) from exc

    @router.post("/reviews/{review_id}/team/release")
    async def _post_release(review_id: str) -> dict[str, Any]:
        try:
            return await run_in_threadpool(decide, release, deps, review_id)
        except TeamRefused as exc:
            raise refused(exc) from exc

    @router.post("/reviews/{review_id}/team/handoff")
    async def _post_handoff(review_id: str, request: Request) -> dict[str, Any]:
        body = await _json_object(request)
        to, note = body.get("to"), body.get("note", "")
        if not isinstance(to, str) or not to.strip() or not isinstance(note, str):
            raise HTTPException(status_code=422, detail="name the teammate and give the note as text")
        try:
            return await run_in_threadpool(decide, hand_off, deps, review_id, to=to.strip(), note=note)
        except TeamRefused as exc:
            raise refused(exc) from exc


__all__ = [
    "Away",
    "RequestMove",
    "Row",
    "TeamAuthority",
    "TeamDeps",
    "TeamRefused",
    "beat",
    "build_status",
    "clear_review",
    "end_for_person",
    "hand_off",
    "register_team_routes",
    "release",
    "row_wire",
    "rows_by_review",
    "take",
    "team_wire",
]
