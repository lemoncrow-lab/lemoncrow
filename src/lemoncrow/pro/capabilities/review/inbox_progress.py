"""Target progress per Review for an Inbox page, stored per (Snapshot, reviewer) (local and hosted).

The stamp is the Snapshot row (which fixes its units and packet), the reviewer's marks, the annotations and the
deriving code. A change to any input derives again; a change of deriving code alone derives again within
``refresh``'s budget (see :class:`DerivedRefresh`). An unchanged Review is one stored read.

A page may also cap how many Reviews it derives (a :class:`DeriveBudget`, shared by every repository the page reads): a
Review left over carries no progress on this read, and the next read derives the next ones. That bounds what one hosted Home load can cost however many Reviews the
viewer has never opened; the local Inbox derives them all, as it always has.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from lemoncrow.pro.capabilities.review.revisions import compute_frontier
from lemoncrow.pro.capabilities.review.store import DerivedResult, ReviewListInputs
from lemoncrow.pro.capabilities.review.targets import (
    DerivedRefresh,
    derivation_stamp,
    derive_review_targets,
    review_progress,
)

_LOG = logging.getLogger(__name__)

EMPTY_PROGRESS: Final[Mapping[str, int]] = {
    "target_count": 0,
    "reviewed": 0,
    "changed_since_review": 0,
    "needs_changes": 0,
    "unreviewed": 0,
    "unknown": 0,
    "mechanical": 0,
    "later": 0,
    "decision": 0,
    "quick_check": 0,
    "accepted_by_rule": 0,
    "decision_open": 0,
    "quick_check_open": 0,
}


type ReaderTargets = Callable[[Any, Any, Sequence[Any], Sequence[Any], Sequence[Any]], Sequence[Any] | None]
"""``(session, revision, units, frontier entries, annotations)`` -> the Reader's own targets, or ``None`` when it has none."""


def answer_progress(targets: Sequence[Any]) -> dict[str, int]:
    """The one count of parts, decisions and quick checks (total and still open) every Review surface shows.

    Home reads it from the Inbox row; the Reader's Overview, Brief and top bar count the same targets the same way
    (``answerCounts`` in the frontend). A part is open until it is reviewed or marked as needing a change.
    """

    progress = review_progress(targets)
    open_parts = [t for t in targets if t.state not in ("reviewed", "needs_changes")]
    return {
        "target_count": progress.target_count,
        "reviewed": progress.reviewed,
        "changed_since_review": progress.changed_since_review,
        "needs_changes": progress.needs_changes,
        "unreviewed": progress.unreviewed,
        "unknown": progress.unknown,
        "mechanical": progress.mechanical,
        "later": progress.later,
        "decision": progress.decision,
        "quick_check": progress.quick_check,
        "accepted_by_rule": progress.accepted_by_rule,
        "decision_open": sum(t.answer.category == "decision" for t in open_parts),
        "quick_check_open": sum(t.answer.category == "quick_check" for t in open_parts),
    }


class DeriveBudget:
    """How many Reviews one request may still derive progress for (``None`` limit: all of them)."""

    __slots__ = ("remaining",)

    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def stored_progress(
    store: Any,
    items: Sequence[tuple[Any, ReviewListInputs]],
    refresh: DerivedRefresh | None = None,
    *,
    read_packet: Callable[[Any, Any], Any],
    budget: DeriveBudget | None = None,
    reader_targets: ReaderTargets | None = None,
) -> dict[str, dict[str, int]]:
    """Target progress per Review id for ``(session, inputs)`` pairs; ``session.reviewer_id`` is whose it is.

    ``reader_targets`` is the Reader's own derivation (plan questions, policy, Routine proofs): with it Home counts the
    decisions the Overview and the Brief show. Without it (or when it has none for a Review) the targets are derived
    from the units, marks and packet alone.
    """

    refresh = refresh if refresh is not None else DerivedRefresh()
    out: dict[str, dict[str, int]] = {}
    pending: list[tuple[Any, ReviewListInputs, str]] = []
    for session, item in items:
        if item.revision is None:
            out[str(session.id)] = dict(EMPTY_PROGRESS)
            continue
        # lc-debt: the Reader's derivation also reads checks, decisions and stored AI questions that this stamp does
        # not cover, so a row can lag them until a mark, annotation or new Snapshot changes; stamp those inputs too.
        stamp = derivation_stamp(
            item.revision, session.reviewer_id, item.marks, item.annotations, "reader" if reader_targets else ""
        )
        pending.append((session, item, stamp))
    stored = store.derived_results("progress", (item.revision.id for _s, item, _t in pending if item.revision))
    fresh: list[DerivedResult] = []
    for session, item, stamp in pending:
        revision = item.revision
        assert revision is not None
        hit = stored.get((revision.id, str(session.reviewer_id)))
        # A row stored before open counts existed reads as stale code: derived again inside the request's
        # budget, served as stored (without them) once the budget is spent.
        legacy = hit is not None and "decision_open" not in hit[1]
        if hit is not None and refresh.serve(hit[0] + "+legacy" if legacy else hit[0], stamp):
            out[str(session.id)] = {key: int(value) for key, value in hit[1].items()}
            continue
        if budget is not None and not budget.take():
            continue
        with refresh.deriving():
            units = store.list_units(revision.id)
            frontier = compute_frontier(
                session.id, session.reviewer_id, revision, units, item.marks, annotations=item.annotations
            )
            targets: Sequence[Any] | None = None
            if reader_targets is not None:
                try:
                    targets = reader_targets(session, revision, units, frontier.entries, item.annotations)
                except Exception:
                    # Home still lists the Review; its counts fall back to the plain derivation below.
                    _LOG.warning("could not derive Reader targets for Review %s", session.id, exc_info=True)
            if targets is None:
                targets = derive_review_targets(
                    units, frontier.entries, read_packet(store, revision), annotations=item.annotations
                )
            result = answer_progress(targets)
        out[str(session.id)] = result
        fresh.append(
            DerivedResult(
                review_id=str(session.id),
                revision_id=revision.id,
                kind="progress",
                scope_key=str(session.reviewer_id),
                stamp=stamp,
                payload=result,
            )
        )
    if fresh:
        # Best effort: what was served is exact either way; a failed write only derives again next time.
        try:
            store.put_derived_results(fresh)
        except store.DRIVER_ERRORS:
            _LOG.warning("could not store derived Review progress", exc_info=True)
    return out


__all__ = ["EMPTY_PROGRESS", "DeriveBudget", "ReaderTargets", "answer_progress", "stored_progress"]
