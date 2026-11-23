"""Whose work a viewer may not read yet (SPEC 4.16 anti-anchoring, §7.1 P7-1 F / P9-4 A).

A second reviewer answers on her own first. Until she has, nothing the first reviewer did may reach her through any
read: not their notes, not their verdict, not a count that includes them, not a flag that says they started. Patching
every payload that counts or lists such things leaves the next one open, so the rule lives where the rows are read:
the store's list methods for annotations, verdicts, marks and activity drop what the hidden people wrote while a
request runs inside :func:`enter`.

The scope is per request: the Review API sets it for a hidden viewer's reads, and nothing else ever does, so writes,
the second-reviewer status itself and every job read the full rows. A ``ContextVar`` is private to the request's task
and to the threads it hands work to; setting it does not reach another request.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from types import MappingProxyType
from typing import Any

_NONE: Mapping[str, frozenset[str]] = MappingProxyType({})
_HIDDEN: ContextVar[Mapping[str, frozenset[str]]] = ContextVar("lemoncrow_review_hidden_authors", default=_NONE)


def enter(review_id: str, authors: Iterable[str]) -> None:
    """Hide *authors* of *review_id* from every read of this request from now on.

    Recorded even for nobody, so :func:`known` can tell "decided: nobody" from "not decided yet". Not undone: a
    request owns its context, so the scope ends with it. Use :func:`scoped` for a part of a request."""

    _HIDDEN.set({**_HIDDEN.get(), review_id: frozenset(authors)})


@contextmanager
def scoped_many(hidden: Mapping[str, Iterable[str]]) -> Iterator[None]:
    """:func:`scoped` for several Reviews at once (an Inbox over many)."""

    token = _HIDDEN.set({**_HIDDEN.get(), **{rid: frozenset(who) for rid, who in hidden.items()}})
    try:
        yield
    finally:
        _HIDDEN.reset(token)


def known(review_id: str) -> bool:
    """Whether this request already decided who is hidden on *review_id* (an empty answer included)."""

    return review_id in _HIDDEN.get()


@contextmanager
def scoped(review_id: str, authors: Iterable[str]) -> Iterator[None]:
    """Hide *authors* only inside the block: for the response a write builds (its decision and its writes ran in
    full before it)."""

    token = _HIDDEN.set({**_HIDDEN.get(), review_id: frozenset(authors)})
    try:
        yield
    finally:
        _HIDDEN.reset(token)


@contextmanager
def unfiltered() -> Iterator[None]:
    """Read the full rows inside a scope. For the code that decides what the lens hides (who was asked, which
    approvals are held): it must see the first reviewer's verdict to know it exists."""

    token = _HIDDEN.set(_NONE)
    try:
        yield
    finally:
        _HIDDEN.reset(token)


_DECISION_ONLY: ContextVar[bool] = ContextVar("lemoncrow_review_decision_only", default=False)


@contextmanager
def decision_read() -> Iterator[None]:
    """Read the full rows to decide something, for a viewer who is shown a narrowed answer.

    Like :func:`unfiltered`, and what is computed here is for the decision alone: it must not be stored as the answer
    that viewer is later shown (:func:`decision_only`)."""

    token = _DECISION_ONLY.set(True)
    try:
        with unfiltered():
            yield
    finally:
        _DECISION_ONLY.reset(token)


def decision_only() -> bool:
    return _DECISION_ONLY.get()


def hidden_in(review_id: str) -> frozenset[str]:
    """Who this request may not read on *review_id* (empty outside a scope)."""

    return _HIDDEN.get().get(review_id, frozenset())


def visible[T](review_id: str, rows: Iterable[T], author_of: Callable[[T], str]) -> tuple[T, ...]:
    """*rows* without those written by someone hidden."""

    hidden = _HIDDEN.get().get(review_id)
    if not hidden:
        return tuple(rows)
    return tuple(row for row in rows if author_of(row) not in hidden)


def notes(review_id: str, rows: Iterable[Any]) -> tuple[Any, ...]:
    return visible(review_id, rows, lambda row: str(getattr(row, "created_by", "") or ""))


def outcomes(review_id: str, rows: Iterable[Any]) -> tuple[Any, ...]:
    return visible(review_id, rows, lambda row: str(getattr(row, "reviewer_id", "") or ""))


def ask_rows(review_id: str, rows: Iterable[Any]) -> tuple[Any, ...]:
    """Ask conversations and turns without the hidden people's, and without any that record no author (a row from before
    authors were recorded cannot be told from theirs: fail closed)."""

    hidden = _HIDDEN.get().get(review_id)
    if not hidden:
        return tuple(rows)
    return tuple(row for row in rows if (author := str(getattr(row, "created_by", "") or "")) and author not in hidden)


def reachable[T](hidden: Iterable[str], found: T | None, author_of: Callable[[T], str]) -> T | None:
    """The one rule for a lookup by id: an object whose author is hidden from this reader is missing, not refused, so an
    id of his answers exactly as an id that never existed. Every route that takes the id of a person-authored object
    (note, parent, action, request, proposal, record ...) goes through this after it has loaded the object; it never
    narrows what a decision reads, only whether this reader may name the object."""

    if found is None:
        return None
    return None if author_of(found) in frozenset(hidden) else found


def ask_author_sql(review_id: str) -> tuple[str, tuple[str, ...]]:
    """The ``AND ...`` that keeps a list query to the Ask rows this reader may see (see :func:`ask_rows`)."""

    hidden = _HIDDEN.get().get(review_id)
    if not hidden:
        return "", ()
    ordered = tuple(sorted(hidden))
    return f" AND created_by != '' AND created_by NOT IN ({', '.join('?' for _ in ordered)})", ordered


def provider_operations(review_id: str, rows: Iterable[Any]) -> tuple[Any, ...]:
    """Outbound provider operations (a publish carries the verdict it posted) without the hidden people's own. A publish
    whose author cannot be read is dropped too: fail closed."""

    hidden = _HIDDEN.get().get(review_id)
    if not hidden:
        return tuple(rows)
    return tuple(
        row
        for row in rows
        if str(getattr(row, "created_by", "") or "") not in hidden
        and not (getattr(row, "kind", "") == "publish_review" and not str(getattr(row, "created_by", "") or ""))
    )


def activity(review_id: str, rows: Iterable[Any]) -> tuple[Any, ...]:
    """Activity without the hidden people's own events, and without the signed record of a person's verdict (the control
    plane writes that event, so its actor is the plane, not the person: it still says that a verdict exists)."""

    hidden = _HIDDEN.get().get(review_id)
    if not hidden:
        return tuple(rows)
    return tuple(
        _redact(row)
        for row in rows
        if str(getattr(row, "actor_id", "") or "") not in hidden
        and not (getattr(row, "kind", "") == "record.exported" and _is_verdict_record(row))
        and not _names_one_of(row, hidden)
        # An outbound provider operation is the person's publish (its state changes carry the provider's own actor, so
        # the actor test above does not see whose): a hidden reader is told of none of them.
        and not str(getattr(row, "kind", "") or "").startswith("provider.operation_")
    )


#: The key an event carries when it was written from the full rows by a viewer who is shown a narrowed answer
#: (:func:`decision_read`): its words and details may name what that viewer must not read yet.
FROM_FULL_ROWS = "from_full_rows"
#: What an event written from the full rows keeps for a viewer who may not read them: that it happened, and the
#: plain facts of what changed. Everything else (a blocker list, who had approved) is dropped.
_KEPT = frozenset({"origin", "generation", "previous_generations", "action", "kind", "state", FROM_FULL_ROWS})


def mark_if_decision(detail_json: str) -> str:
    """The event's detail, marked when it is written inside :func:`decision_read` (any kind: fail closed)."""

    if not _DECISION_ONLY.get():
        return detail_json
    try:
        detail = json.loads(detail_json or "{}")
    except ValueError:
        return detail_json
    if not isinstance(detail, dict):
        return detail_json
    return json.dumps({**detail, FROM_FULL_ROWS: True}, sort_keys=True, ensure_ascii=False)


#: Event kinds a decision derives from readiness (the rows a hidden reader may not read yet). Whoever wrote
#: one, and whether or not it carries the mark, a hidden reader sees it only through the allowlist above: the mark is a
#: second line, the reader is the first.
_DERIVED_PREFIXES = ("policy.", "landing.", "premerge.", "automation.")


def _derived_from_readiness(row: Any) -> bool:
    kind = str(getattr(row, "kind", "") or "")
    if kind.startswith(_DERIVED_PREFIXES):
        return True
    try:
        detail = json.loads(str(getattr(row, "detail_json", "") or "{}"))
    except ValueError:
        return True  # unreadable: say as little as possible
    return isinstance(detail, dict) and bool(detail.get(FROM_FULL_ROWS))


def _redact(row: Any) -> Any:
    """A readiness-derived event as a hidden reader reads it: still there (she learns it happened), without the words."""

    if not _derived_from_readiness(row):
        return row
    try:
        detail = json.loads(str(getattr(row, "detail_json", "") or "{}"))
    except ValueError:
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    summary = str(getattr(row, "summary", "") or "")
    head, found, _ = summary.partition("): ")
    neutral = head + ")" if found else f"{getattr(row, 'kind', 'Review')} recorded"
    kept = {key: value for key, value in detail.items() if key in _KEPT}
    return replace(row, summary=neutral, detail_json=json.dumps(kept, sort_keys=True, ensure_ascii=False))


#: What a hidden reader keeps of a landing (Merge when ready) intent's stored metadata: where it stands and how a
#: withdrawal was settled. Not the blockers, the free-text details, the conditions it was armed under or who armed it.
_LANDING_KEPT = frozenset({"provider", "provider_cancel", "cancel_reason", "cancel_state", "provider_state", "state"})
_LANDING_NEUTRAL_BY_STATE = {
    "armed": "Merge when ready is on.",
    "delegated": "Merge when ready is on.",
    "queued": "Merge when ready is on.",
    "merged": "",
}
LANDING_NEUTRAL = "Merge when ready is not in force."


def landing_neutral(state: str) -> str:
    """The one line a hidden reader gets for a landing in *state* (an armed landing is on, a merged one needs no words)."""

    return _LANDING_NEUTRAL_BY_STATE.get(state, LANDING_NEUTRAL)


def landing_metadata_wire(review_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    """A landing row's metadata for a reader: whole, or the allowlist while anything is hidden from her."""

    if not _HIDDEN.get().get(review_id):
        return dict(metadata)
    return {key: value for key, value in metadata.items() if key in _LANDING_KEPT}


def landing_wire(review_id: str, wire: Mapping[str, Any]) -> dict[str, Any]:
    """The readiness answer's ``landing`` block for a reader: its free text and basis details are the withdrawal's own
    words (the blockers, the old policy), so a hidden reader gets a neutral line instead."""

    if not _HIDDEN.get().get(review_id):
        return dict(wire)
    out = dict(wire)
    if out.get("detail"):
        out["detail"] = landing_neutral(str(out.get("state") or ""))
    if "bypassed" in out:
        out["bypassed"] = []
    auto = out.get("auto_approval")
    if isinstance(auto, Mapping) and auto.get("detail"):
        out["auto_approval"] = {**auto, "detail": landing_neutral(str(auto.get("state") or ""))}
    return out


def _names_one_of(row: Any, hidden: frozenset[str]) -> bool:
    """A request or attention event about a hidden person (the author's act, but it is their review work)."""

    kind = str(getattr(row, "kind", "") or "")
    if not kind.startswith(("request.", "attention.")):
        return False
    text = f"{getattr(row, 'summary', '')} {getattr(row, 'subject_id', '')} {getattr(row, 'detail_json', '')}"
    return any(person in text for person in hidden)


def _is_verdict_record(row: Any) -> bool:
    try:
        detail = json.loads(str(getattr(row, "detail_json", "") or "{}"))
    except ValueError:
        return True  # unreadable: say nothing rather than guess
    return not isinstance(detail, dict) or detail.get("decision_kind") == "human_outcome"


def reviewer_marks[T](review_id: str, reviewer_id: str, rows: Iterable[T]) -> tuple[T, ...]:
    """One reviewer's marks, or none when that reviewer is hidden."""

    return () if reviewer_id in _HIDDEN.get().get(review_id, frozenset()) else tuple(rows)


def attention[T](
    review_id: str,
    rows: Iterable[T],
    principal_of: Callable[[T], str],
    by: Callable[[T], str],
    unattributed: Callable[[T], T | None] | None = None,
) -> tuple[T, ...]:
    """Attention rows (who is next, who caused it) without the hidden people's own and without those they caused.

    A row they caused is dropped, except one ``unattributed`` can keep: it returns the row with the hidden person's
    name taken out, or ``None`` to drop it. That is for a row that says only that someone is asked to do something (a
    request addressed to the reader), which she must still be shown; it names no word or judgment of theirs."""

    hidden = _HIDDEN.get().get(review_id)
    if not hidden:
        return tuple(rows)
    kept: list[T] = []
    for row in rows:
        if principal_of(row) in hidden:
            continue
        if by(row) in hidden:
            anonymous = unattributed(row) if unattributed is not None else None
            if anonymous is None:
                continue
            row = anonymous
        kept.append(row)
    return tuple(kept)


def notes_of_any_review(rows: Iterable[Any]) -> tuple[Any, ...]:
    """A repository-wide list of notes (rule suggestions are made from it) without those a hidden person wrote on a
    Review where they are hidden."""

    scope = _HIDDEN.get()
    if not any(scope.values()):
        return tuple(rows)
    return tuple(
        row for row in rows if str(getattr(row, "created_by", "") or "") not in scope.get(str(row.review_id), ())
    )
