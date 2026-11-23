"""Personal attention: how much one Review needs *this viewer*, separate from priority (Phase 9 item 6).

Global ``priority`` (P9-PRIO) says what the organization should open first;
personal attention orders one viewer's Inbox. It reads, never writes: the
result is a score plus every factor that contributed, so the order is
explainable and nothing here can change priority, readiness, assignment or
permissions.

Factors (integer points, so equal evidence always yields an equal score):

* **next-action ownership** -- the strongest ReviewAttention reason the viewer
  holds (returned work outranks a fresh request, which outranks a mention);
* **waiting age** -- how long the viewer's oldest reason has waited;
* **activity** -- the Review changed in the last day;
* **assignment / team** -- a direct request, else a request to one of the
  viewer's teams (actual requests only, never suggestions);
* **engagement** -- the viewer already participates;
* **impact** -- the Review's priority and criticality (read, never changed);
* **expected review effort** -- smaller remaining work first, when known.

Order: score descending, then the oldest waiting reason first, then repository
and Review id. The id tiebreak makes the order total and deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from lemoncrow.pro.capabilities.review.freshness import parse_timestamp

#: Points for the strongest reason kind the viewer holds.
OWNERSHIP_POINTS: Final[Mapping[str, int]] = {
    "thread_returned": 40,
    "work_returned": 40,
    "review_requested": 30,
    "review_pending": 30,
    "feedback_received": 30,
    "changes_requested": 30,
    "check_failed": 30,
    "landing_attention": 25,
    "manual": 25,
    "mention": 20,
    "reply": 20,
    "scope_reconciliation": 15,
}
PRIORITY_POINTS: Final[Mapping[str, int]] = {"urgent": 15, "high": 10, "normal": 0, "low": 0}
CRITICALITY_POINTS: Final[Mapping[str, int]] = {"critical": 5, "high": 3, "normal": 0, "low": 0}
_AGE_STEPS: Final[tuple[tuple[timedelta, int], ...]] = (
    (timedelta(days=7), 15),
    (timedelta(days=3), 10),
    (timedelta(days=1), 5),
)
_RECENT: Final[timedelta] = timedelta(days=1)
_EFFORT_STEPS: Final[tuple[tuple[int, int, str], ...]] = ((5, 5, "small"), (20, 2, "medium"))


@dataclass(frozen=True, slots=True)
class ViewerReason:
    """One reason the viewer is the next actor: kind, durable fact text and since when."""

    kind: str
    since: str = ""
    fact: str = ""


@dataclass(frozen=True, slots=True)
class PersonalAttentionInputs:
    open: bool
    reasons: tuple[ViewerReason, ...] = ()
    updated_at: str = ""
    direct_request: bool = False
    team_request: bool = False
    engaged: bool = False
    priority: str = "normal"
    criticality: str = "normal"
    effort: int | None = None
    effort_unit: str = "targets"


@dataclass(frozen=True, slots=True)
class AttentionFactor:
    key: str
    label: str
    points: int
    detail: str

    def to_wire(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "points": self.points, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class PersonalAttention:
    score: int
    factors: tuple[AttentionFactor, ...]
    waiting_since: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "waiting_since": self.waiting_since,
            "factors": [factor.to_wire() for factor in self.factors],
        }


def _age_points(since: str, now: datetime) -> tuple[int, str]:
    instant = parse_timestamp(since)
    if instant is None:
        return 0, "waiting time unknown"
    age = max(timedelta(0), now - instant)
    days = age.total_seconds() / 86400
    for threshold, points in _AGE_STEPS:
        if age >= threshold:
            return points, f"waiting {days:.0f} day(s)"
    return 0, "waiting under a day"


def personal_attention(inputs: PersonalAttentionInputs, *, now: datetime) -> PersonalAttention:
    """Score one Review for one viewer; every non-zero contribution is a listed factor."""

    factors: list[AttentionFactor] = []
    waiting_since = ""
    if inputs.open and inputs.reasons:
        strongest = max(inputs.reasons, key=lambda item: (OWNERSHIP_POINTS.get(item.kind, 10), item.kind))
        factors.append(
            AttentionFactor(
                "ownership",
                "You own the next action",
                OWNERSHIP_POINTS.get(strongest.kind, 10),
                strongest.fact or strongest.kind,
            )
        )
        stamps = sorted(item.since for item in inputs.reasons if parse_timestamp(item.since) is not None)
        if stamps:
            waiting_since = stamps[0]
            points, detail = _age_points(waiting_since, now)
            if points:
                factors.append(AttentionFactor("waiting_age", "Waiting on you", points, detail))
    updated = parse_timestamp(inputs.updated_at)
    if inputs.open and updated is not None and now - updated <= _RECENT:
        factors.append(AttentionFactor("activity", "Recent activity", 5, "changed in the last day"))
    if inputs.open and inputs.direct_request:
        factors.append(AttentionFactor("assignment", "Requested from you", 10, "direct review request"))
    elif inputs.open and inputs.team_request:
        factors.append(AttentionFactor("assignment", "Requested from your team", 5, "team review request"))
    if inputs.open and inputs.engaged:
        factors.append(AttentionFactor("engagement", "You participate", 5, "already involved in this Review"))
    impact = PRIORITY_POINTS.get(inputs.priority, 0) + CRITICALITY_POINTS.get(inputs.criticality, 0)
    if inputs.open and impact:
        factors.append(
            AttentionFactor("impact", "Impact", impact, f"priority {inputs.priority}, criticality {inputs.criticality}")
        )
    if inputs.open and inputs.effort is not None and inputs.effort > 0:
        for ceiling, points, size in _EFFORT_STEPS:
            if inputs.effort <= ceiling:
                unit = inputs.effort_unit[:-1] if inputs.effort == 1 else inputs.effort_unit
                factors.append(AttentionFactor("effort", "Expected effort", points, f"{size}: {inputs.effort} {unit}"))
                break
    return PersonalAttention(
        score=sum(factor.points for factor in factors),
        factors=tuple(factors),
        waiting_since=waiting_since,
    )


def attention_sort_key(attention: PersonalAttention, *, repo_id: str, review_id: str) -> tuple[Any, ...]:
    """Total, deterministic order: score, oldest wait first, then repository and Review id."""

    waited = parse_timestamp(attention.waiting_since)
    # Unknown waits sort after every known one; ISO strings of one format compare chronologically.
    since = "~" if waited is None else waited.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return (-attention.score, since, repo_id, review_id)


def order_by_attention(rows: Sequence[tuple[PersonalAttention, str, str]]) -> list[tuple[PersonalAttention, str, str]]:
    """Order ``(attention, repo_id, review_id)`` triples by :func:`attention_sort_key`."""

    return sorted(rows, key=lambda row: attention_sort_key(row[0], repo_id=row[1], review_id=row[2]))


__all__ = [
    "CRITICALITY_POINTS",
    "OWNERSHIP_POINTS",
    "PRIORITY_POINTS",
    "AttentionFactor",
    "PersonalAttention",
    "PersonalAttentionInputs",
    "ViewerReason",
    "attention_sort_key",
    "order_by_attention",
    "personal_attention",
]
