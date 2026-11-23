"""Bulk, body-free Inbox inputs shared by the local and hosted Inbox (P9-INBOX).

Each loader is one store read per 500 Snapshots, whatever the Review count, and
reads only stored projections: the P9-PRIO triage row plus its override events,
the readiness record, the fact watermark, the post-merge ledger rows and the
break-glass records (outcomes only for the rare Reviews that have one). No
packet, unit, source or comment body is opened.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lemoncrow.pro.capabilities.review.post_merge_actions import POST_MERGE_ROW_KIND, TERMINAL_STATUSES
from lemoncrow.pro.capabilities.review.review_triage import (
    PRIORITY_OVERRIDE_KINDS,
    TRIAGE_KIND,
    TRIAGE_SCOPE,
    ReviewTriage,
    active_override,
    missing_triage,
    with_override,
)
from lemoncrow.pro.capabilities.review.workflow_state import ReadinessFact, readiness_fact, readiness_summaries


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    review_id: str
    revision_id: str


@dataclass(frozen=True)
class StoredInboxFacts:
    """Per Review: triage (priority), readiness fact and running post-merge actions."""

    triage: Mapping[str, ReviewTriage]
    readiness: Mapping[str, ReadinessFact]
    post_merge_running: Mapping[str, int]


def load_stored_inbox_facts(store: Any, refs: Sequence[SnapshotRef], *, viewer: str, now: datetime) -> StoredInboxFacts:
    """Six bulk reads (triage, override events, readiness, watermarks, post-merge, break-glass) for every Review.

    An open break-glass follow-up counts as a running post-merge action.

    ``viewer`` selects whose readiness record is read (readiness is evaluated as a reviewer).
    """

    current = [ref for ref in refs if ref.revision_id]
    revision_ids = [ref.revision_id for ref in current]
    review_ids = [ref.review_id for ref in current]
    triage_rows = store.derived_results(TRIAGE_KIND, revision_ids) if revision_ids else {}
    events = store.activity_events_for_reviews(review_ids, kinds=PRIORITY_OVERRIDE_KINDS) if review_ids else {}
    summaries = readiness_summaries(store, revision_ids, evaluator=viewer) if revision_ids else {}
    watermarks = store.fact_watermarks(review_ids) if review_ids else {}
    post_merge = store.derived_results(POST_MERGE_ROW_KIND, revision_ids) if revision_ids else {}
    running: dict[str, int] = {}
    by_revision = {ref.revision_id: ref.review_id for ref in current}
    for (revision_id, _scope), (_stamp, payload) in post_merge.items():
        if str(payload.get("status") or "") not in TERMINAL_STATUSES:
            review_id = by_revision.get(revision_id, "")
            running[review_id] = running.get(review_id, 0) + 1
    for review_id, count in _open_follow_ups(store, revision_ids, by_revision, now=now).items():
        running[review_id] = running.get(review_id, 0) + count
    triage: dict[str, ReviewTriage] = {}
    readiness: dict[str, ReadinessFact] = {}
    for ref in refs:
        if not ref.revision_id:
            triage[ref.review_id] = missing_triage(ref.review_id, "")
            readiness[ref.review_id] = ReadinessFact()
            continue
        row = triage_rows.get((ref.revision_id, TRIAGE_SCOPE))
        stored = ReviewTriage.from_wire(row[1]) if row is not None else None
        if stored is None or stored.review_id != ref.review_id:
            stored = missing_triage(ref.review_id, ref.revision_id)
        triage[ref.review_id] = with_override(
            stored, active_override(events.get(ref.review_id, ()), current_revision_id=ref.revision_id)
        )
        readiness[ref.review_id] = readiness_fact(
            summaries.get(ref.revision_id),
            revision_id=ref.revision_id,
            watermark=watermarks.get(ref.review_id, ""),
            now=now,
        )
    return StoredInboxFacts(triage=triage, readiness=readiness, post_merge_running=running)


def _open_follow_ups(
    store: Any, revision_ids: Sequence[str], by_revision: Mapping[str, str], *, now: datetime
) -> dict[str, int]:
    """Open or overdue break-glass follow-up reviews per Review (they keep a merged Review in view).

    One bulk read of the break-glass records; outcomes are read only for the
    Reviews that have one, since completion is derived from outcomes.
    """

    from lemoncrow.pro.capabilities.review.break_glass import RECORD_KIND, follow_up_state

    rows = store.derived_results(RECORD_KIND, list(revision_ids)) if revision_ids else {}
    found: dict[str, list[Mapping[str, Any]]] = {}
    for (revision_id, _scope), (_stamp, payload) in rows.items():
        review_id = by_revision.get(revision_id, "")
        if review_id:
            found.setdefault(review_id, []).append(payload)
    # lc-debt: one outcome read per Review with a break-glass record (rare by design); upgrade
    # path: a bulk outcomes-by-revision store read if break-glass becomes routine.
    open_count: dict[str, int] = {}
    for review_id, records in found.items():
        from lemoncrow.pro.capabilities.review.record_retraction import retracted_outcome_ids

        retracted = retracted_outcome_ids(store, review_id)
        outcomes = [item for item in store.list_outcomes(review_id) if item.id not in retracted]
        count = sum(follow_up_state(record, outcomes, now=now)["state"] != "completed" for record in records)
        if count:
            open_count[review_id] = count
    return open_count


def triage_wire(triage: ReviewTriage) -> dict[str, Any]:
    """The Inbox's priority field: level plus why, never finding content."""

    return {
        "level": triage.priority,
        "computed": triage.computed,
        "computed_level": triage.computed_priority,
        "criticality": triage.criticality,
        "evidence_state": triage.evidence_state,
        "overridden": triage.override is not None,
        "revision_id": triage.revision_id,
    }


__all__ = ["SnapshotRef", "StoredInboxFacts", "load_stored_inbox_facts", "triage_wire"]
