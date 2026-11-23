"""Review workflow state, derived independently from priority (Phase 9 item 2, P9-INBOX).

One Review-level field that says *where the work stands*:

``needs_review | waiting_on_reviewer | waiting_on_author | waiting_on_checks |
needs_decision | ready_to_merge | monitoring | closed``

It sits next to -- and is never merged with -- the P9-PRIO ``priority`` (what to
open first), the ReviewAttention next-action owner (who acts next) and the
P10-READY readiness record (whether merge conditions hold). Each of those can
change while the others stay put.

Derivation is deterministic and ordered (first matching rule wins, and the rule
name is returned so the decision is explainable):

1. ``closed`` / ``monitoring`` -- a finished, archived or merged Review is
   ``monitoring`` only while a post-merge action is still running, else
   ``closed``.
2. A reviewer holds the turn (reviewer-side ReviewAttention reasons or threads
   returned to a reviewer): ``waiting_on_reviewer`` when the Review already
   carries review evidence, else ``needs_review``.
3. An author or authoring agent holds the turn (author-side reasons, request-
   change threads waiting on the author, feedback handed to an agent, a failed
   required check): ``waiting_on_author``.
4. A person must decide something the facts name (landing attention, scope
   reconciliation, a manual attention reason): ``needs_decision``.
5. Review is not complete on the current Snapshot: ``needs_review``. Missing
   evidence never reads as safe -- an unreviewed Review with no findings lands
   here, never in ``ready_to_merge``.
6. Required checks are pending on a fresh readiness record: ``waiting_on_checks``.
7. The readiness record is fresh, current-Snapshot and ready: ``ready_to_merge``.
   This is the only route to ``ready_to_merge``.
8. Otherwise ``needs_decision``: review is complete but readiness is not ready,
   stale or was never evaluated, so a person decides.

The readiness record is the compact summary the Reader persists whenever it
evaluates readiness (:func:`persist_readiness_summary`). Readiness is evaluated
*as a reviewer* (``latest-snapshot-reviewed`` counts that reviewer's judgment),
so the record is kept per evaluator and the Inbox reads only the viewer's own.
It is fresh only when it belongs to the current Snapshot, was evaluated no
earlier than the newest durable Review fact (the *watermark*: marks, comments,
outcomes, evidence, relationships; hosted adds requests and provider facts) and
its time-sensitive evidence has not expired. Anything else is ``stale`` or
``unknown`` and can never produce ``ready_to_merge``.

Writes: one ``review_derived`` row per (Snapshot, evaluator) (kind
``readiness_summary``); the Snapshot's deletion cascade owns its retention.
Nothing here writes readiness conditions, merge eligibility, assignment or
permissions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.freshness import parse_timestamp

WorkflowState = Literal[
    "needs_review",
    "waiting_on_reviewer",
    "waiting_on_author",
    "waiting_on_checks",
    "needs_decision",
    "ready_to_merge",
    "monitoring",
    "closed",
]
WORKFLOW_STATES: Final[tuple[str, ...]] = (
    "needs_review",
    "waiting_on_reviewer",
    "waiting_on_author",
    "waiting_on_checks",
    "needs_decision",
    "ready_to_merge",
    "monitoring",
    "closed",
)
WORKFLOW_LABELS: Final[Mapping[str, str]] = {
    "needs_review": "Needs review",
    "waiting_on_reviewer": "Waiting on reviewer",
    "waiting_on_author": "Waiting on author",
    "waiting_on_checks": "Waiting on checks",
    "needs_decision": "Needs decision",
    "ready_to_merge": "Ready to merge",
    "monitoring": "Monitoring",
    "closed": "Closed",
}

#: ReviewAttention reason kinds by the side of the Review that owns the turn.
REVIEWER_REASON_KINDS: Final[frozenset[str]] = frozenset({"review_requested", "work_returned", "thread_returned"})
AUTHOR_REASON_KINDS: Final[frozenset[str]] = frozenset({"feedback_received", "changes_requested"})
CHECK_REASON_KINDS: Final[frozenset[str]] = frozenset({"check_failed"})
DECISION_REASON_KINDS: Final[frozenset[str]] = frozenset({"landing_attention", "scope_reconciliation", "manual"})

READINESS_SUMMARY_KIND: Final[str] = "readiness_summary"
READINESS_SUMMARY_SCOPE: Final[str] = "current"
READINESS_SUMMARY_VERSION: Final[int] = 1
READINESS_BLOCKER_WIRE_LIMIT: Final[int] = 20
"""Blocker rows kept in the summary; ``blocker_count`` is always exact."""

ReadinessState = Literal["ready", "not_ready", "stale", "unknown"]
_SATISFIED: Final[frozenset[str]] = frozenset({"pass", "not_applicable", "waived"})
_CHECK_CATEGORIES: Final[frozenset[str]] = frozenset({"verification", "scm"})
_REVIEWED_CONDITION: Final[str] = "latest-snapshot-reviewed"


# --------------------------------------------------------------------------- #
# readiness record (P10-READY summary, lightweight)
# --------------------------------------------------------------------------- #


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def readiness_summary_payload(readiness: Mapping[str, Any], *, review_id: str, revision_id: str) -> dict[str, Any]:
    """The compact, content-free readiness record for one Snapshot.

    Only condition identity/owner/category/state survive; labels, details and
    evidence refs stay with the Reader.
    """

    conditions = [row for row in readiness.get("conditions") or () if isinstance(row, Mapping)]
    blockers = [row for row in conditions if str(row.get("state") or "") not in _SATISFIED]
    generation = readiness.get("policy_generation")
    return {
        "version": READINESS_SUMMARY_VERSION,
        "review_id": review_id,
        "revision_id": revision_id,
        "ready": bool(readiness.get("ready")) and not blockers,
        "blocker_count": len(blockers),
        "scm_blockers": sum(str(row.get("owner") or "") == "scm" for row in blockers),
        # Positive SCM evidence: how many check/SCM conditions were evaluated at all.
        "scm_conditions": sum(
            str(row.get("category") or "") in _CHECK_CATEGORIES or str(row.get("owner") or "") == "scm"
            for row in conditions
        ),
        "pending_checks": sum(
            str(row.get("state") or "") == "pending"
            and (str(row.get("category") or "") in _CHECK_CATEGORIES or str(row.get("owner") or "") == "scm")
            for row in blockers
        ),
        "failed_checks": sum(
            str(row.get("state") or "") == "fail"
            and (str(row.get("category") or "") in _CHECK_CATEGORIES or str(row.get("owner") or "") == "scm")
            for row in blockers
        ),
        "reviewed": any(
            str(row.get("id") or "") == _REVIEWED_CONDITION and str(row.get("state") or "") == "pass"
            for row in conditions
        ),
        "blockers": [
            {
                "id": str(row.get("id") or ""),
                "owner": str(row.get("owner") or "lemoncrow"),
                "category": str(row.get("category") or ""),
                "state": str(row.get("state") or ""),
            }
            for row in blockers[:READINESS_BLOCKER_WIRE_LIMIT]
        ],
        "conditions_hash": str(readiness.get("conditions_hash") or ""),
        "policy_generation": (
            str(generation.get("digest") or "") if isinstance(generation, Mapping) else str(generation or "")
        ),
        "evaluated_at": str(readiness.get("evaluated_at") or ""),
        "valid_until": str(readiness.get("valid_until") or ""),
    }


def _later(left: str, right: str) -> bool:
    """``left`` is strictly later than ``right``; an unreadable ``left`` is never later."""

    parsed_left = parse_timestamp(left)
    parsed_right = parse_timestamp(right)
    if parsed_left is None:
        return False
    if parsed_right is None:
        return True
    return parsed_left > parsed_right


def readiness_scope(evaluator: str) -> str:
    """Readiness is evaluated *as* a reviewer (``latest-snapshot-reviewed`` counts their judgment)."""

    return f"{READINESS_SUMMARY_SCOPE}:{evaluator}"


def persist_readiness_summary(
    store: Any,
    *,
    review_id: str,
    revision_id: str,
    readiness: Mapping[str, Any],
    evaluator: str,
    watermark: str = "",
) -> bool:
    """Store ``evaluator``'s readiness record for its Snapshot; ``True`` when a row was written.

    Rewritten only when the answer changed or the stored evaluation predates
    ``watermark`` (the newest durable Review fact), so a polled readiness read
    does not write on every request. One row per (Snapshot, evaluator): the
    Inbox reads only the viewer's own record, never another person's.
    Closer: Snapshot deletion cascades ``review_derived``.
    """

    from lemoncrow.pro.capabilities.review.store import DerivedResult

    scope = readiness_scope(evaluator)
    payload = readiness_summary_payload(readiness, review_id=review_id, revision_id=revision_id)
    answer = {key: value for key, value in payload.items() if key != "evaluated_at"}
    stamp = hashlib.sha256(_canonical(answer)).hexdigest()
    existing = store.derived_results(READINESS_SUMMARY_KIND, [revision_id]).get((revision_id, scope))
    if existing is not None and existing[0] == stamp:
        stored_at = str(existing[1].get("evaluated_at") or "")
        if not watermark or not _later(watermark, stored_at):
            return False
    store.put_derived_results(
        [
            DerivedResult(
                review_id=review_id,
                revision_id=revision_id,
                kind=READINESS_SUMMARY_KIND,
                scope_key=scope,
                stamp=stamp,
                payload=payload,
            )
        ]
    )
    return True


def readiness_summaries(store: Any, revision_ids: Iterable[str], *, evaluator: str) -> dict[str, Mapping[str, Any]]:
    """``evaluator``'s stored readiness records by revision id, in one bulk read."""

    # lc-debt: reads every evaluator's row of these Snapshots and keeps one; upgrade path: a
    # derived_results variant filtered by scope_key when Snapshots gather many evaluators.
    scope = readiness_scope(evaluator)
    rows = store.derived_results(READINESS_SUMMARY_KIND, list(revision_ids))
    return {
        revision_id: payload
        for (revision_id, row_scope), (_stamp, payload) in rows.items()
        if row_scope == scope and payload.get("version") == READINESS_SUMMARY_VERSION
    }


@dataclass(frozen=True, slots=True)
class ReadinessFact:
    """What the Inbox may say about readiness; only ``ready`` can make a Review ready to merge."""

    state: ReadinessState = "unknown"
    reason: str = "Readiness was not evaluated for this revision"
    evaluated_at: str = ""
    valid_until: str = ""
    blocker_count: int = 0
    scm_blockers: int = 0
    pending_checks: int = 0
    failed_checks: int = 0
    scm_conditions: int = 0
    reviewed: bool = False
    blocking: tuple[Mapping[str, str], ...] = ()
    """Exact blocking reasons ``{id, owner, state, action}`` from the record's content-free rows."""

    @property
    def fresh(self) -> bool:
        return self.state in {"ready", "not_ready"}

    def to_wire(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "evaluated_at": self.evaluated_at,
            "valid_until": self.valid_until,
            "blocker_count": self.blocker_count,
            "scm_blockers": self.scm_blockers,
            "pending_checks": self.pending_checks,
            "failed_checks": self.failed_checks,
            "scm_conditions": self.scm_conditions,
            "blocking": [dict(row) for row in self.blocking],
        }


def _blocking_reason(payload: Mapping[str, Any], blocker_count: int) -> tuple[tuple[Mapping[str, str], ...], str]:
    """The record's blocker rows in product words, and the Inbox sentence naming them.

    Reads only the content-free rows (id, owner, category, state). The record
    keeps at most :data:`READINESS_BLOCKER_WIRE_LIMIT` rows; blockers past that
    are counted as "more" so the sentence never claims fewer than exist.
    """

    from lemoncrow.pro.capabilities.review.blocking_reasons import blocking_reasons, summary_text

    rows = [row for row in payload.get("blockers") or () if isinstance(row, Mapping)]
    reasons = blocking_reasons(rows, limit=READINESS_BLOCKER_WIRE_LIMIT)
    distinct = len({row["action"] for row in reasons})
    text = summary_text(reasons, total=distinct + max(0, blocker_count - len(rows)))
    if not text:
        return (), f"{blocker_count} readiness condition(s) not satisfied"
    failing = any(row["state"] == "fail" for row in reasons)
    return tuple(reasons), ("Blocked: " if failing else "Waiting: ") + text


def readiness_fact(
    payload: Mapping[str, Any] | None,
    *,
    revision_id: str,
    watermark: str,
    now: datetime,
) -> ReadinessFact:
    """Classify one stored readiness record against the current Snapshot and facts."""

    if payload is None or not revision_id:
        return ReadinessFact()
    if str(payload.get("revision_id") or "") != revision_id:
        return ReadinessFact(reason="Readiness was evaluated on an earlier revision")
    evaluated_at = str(payload.get("evaluated_at") or "")
    valid_until = str(payload.get("valid_until") or "")
    base = ReadinessFact(
        evaluated_at=evaluated_at,
        valid_until=valid_until,
        blocker_count=int(payload.get("blocker_count") or 0),
        scm_blockers=int(payload.get("scm_blockers") or 0),
        pending_checks=int(payload.get("pending_checks") or 0),
        failed_checks=int(payload.get("failed_checks") or 0),
        scm_conditions=int(payload.get("scm_conditions") or 0),
    )
    if parse_timestamp(evaluated_at) is None:
        return replace(base, reason="Readiness record has no evaluation time")
    if watermark and _later(watermark, evaluated_at):
        return replace(
            base,
            state="stale",
            reason="Review activity after the last readiness evaluation; open the Review to re-check",
        )
    expiry = parse_timestamp(valid_until)
    if expiry is not None and now >= expiry:
        return replace(base, state="stale", reason="Time-sensitive readiness evidence expired")
    ready = payload.get("ready") is True and base.blocker_count == 0
    if ready:
        return replace(
            base,
            state="ready",
            reason="All readiness conditions hold for the current revision",
            reviewed=payload.get("reviewed") is True,
        )
    blocking, reason = _blocking_reason(payload, base.blocker_count)
    return replace(base, state="not_ready", reason=reason, reviewed=payload.get("reviewed") is True, blocking=blocking)


# --------------------------------------------------------------------------- #
# workflow derivation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WorkflowFacts:
    """Explicit durable facts one Review's workflow state derives from.

    Counts are over the current Snapshot. ``review_complete`` is true only on
    positive evidence: every live request completed, or (with no requests) the
    reviewer's judgment covers every current target.
    """

    status: str = "open"
    merged: bool = False
    provider_closed: bool = False
    """The linked pull request was closed without merging."""
    post_merge_running: int = 0
    reviewer_reasons: int = 0
    author_reasons: int = 0
    check_failures: int = 0
    decision_reasons: int = 0
    threads_returned: int = 0
    threads_waiting_on_author: int = 0
    feedback_with_agent: int = 0
    review_evidence: bool = False
    review_complete: bool = False
    author_is_agent: bool = False
    readiness: ReadinessFact = ReadinessFact()


@dataclass(frozen=True, slots=True)
class WorkflowDecision:
    state: WorkflowState
    rule: str
    explanation: str

    @property
    def label(self) -> str:
        if self.state == "waiting_on_author" and self.rule == "agent_turn":
            return "Waiting on agent"
        return WORKFLOW_LABELS[self.state]

    def to_wire(self) -> dict[str, str]:
        return {"state": self.state, "label": self.label, "rule": self.rule, "explanation": self.explanation}


def derive_workflow_state(facts: WorkflowFacts) -> WorkflowDecision:
    """The ordered rules of the module docstring; the first match decides."""

    if facts.status in {"finished", "archived"} or facts.merged or facts.provider_closed:
        if facts.merged and facts.post_merge_running > 0:
            return WorkflowDecision(
                "monitoring",
                "post_merge_running",
                f"Merged; {facts.post_merge_running} post-merge action(s) still running",
            )
        if facts.merged:
            reason = "Merged"
        elif facts.status in {"finished", "archived"}:
            reason = "Finished" if facts.status == "finished" else "Archived"
        else:
            reason = "Pull request closed without merging"
        return WorkflowDecision("closed", "closed", f"{reason}; no post-merge activity is running")
    reviewer_turn = facts.reviewer_reasons + facts.threads_returned
    if reviewer_turn > 0:
        if facts.review_evidence:
            return WorkflowDecision(
                "waiting_on_reviewer", "reviewer_turn", f"A reviewer holds the next action ({reviewer_turn} reason(s))"
            )
        return WorkflowDecision("needs_review", "review_requested", "Review requested; no review has happened yet")
    author_turn = facts.author_reasons + facts.threads_waiting_on_author + facts.feedback_with_agent
    if author_turn > 0 or facts.check_failures > 0:
        parts = []
        if author_turn:
            parts.append(f"{author_turn} feedback item(s) with the author")
        if facts.check_failures:
            parts.append(f"{facts.check_failures} required check(s) failed")
        agent = facts.author_is_agent or (facts.feedback_with_agent > 0 and facts.author_reasons == 0)
        return WorkflowDecision("waiting_on_author", "agent_turn" if agent else "author_turn", "; ".join(parts))
    if facts.decision_reasons > 0:
        return WorkflowDecision(
            "needs_decision", "decision_reason", f"{facts.decision_reasons} item(s) need a person's decision"
        )
    if not facts.review_complete:
        return WorkflowDecision(
            "needs_review",
            "not_reviewed",
            "Review is not complete on the current revision (missing evidence is not evidence of safety)",
        )
    readiness = facts.readiness
    if readiness.fresh and readiness.pending_checks > 0:
        return WorkflowDecision(
            "waiting_on_checks", "checks_pending", f"{readiness.pending_checks} required check(s) pending"
        )
    if readiness.state == "ready":
        return WorkflowDecision("ready_to_merge", "readiness_ready", readiness.reason)
    return WorkflowDecision("needs_decision", "readiness_" + readiness.state, f"Review complete; {readiness.reason}")


def scm_axis(
    readiness: ReadinessFact,
    *,
    connected: bool,
    provider: str = "",
    sync_state: str = "",
    failed_check_reasons: int = 0,
) -> dict[str, Any]:
    """The Inbox's SCM-readiness axis from stored facts only (no provider call, no packet).

    ``ready`` needs positive evidence: a fresh readiness record that evaluated
    check/SCM conditions and found none blocking, with the provider in sync. A
    failure from either source is ``not_ready``. Without a fresh record the
    Inbox does not know which checks are required, so it says ``unknown``.
    A Review with no linked provider is ``not_connected`` whatever its local
    checks say: nothing here can merge it, so the axis claims nothing about
    merging (the failed/pending counts stay on the wire).
    """

    fresh = readiness.fresh
    failed = failed_check_reasons + (readiness.failed_checks if fresh else 0)
    pending = readiness.pending_checks if fresh else 0
    if not connected:
        state = "not_connected"
    elif failed or (fresh and (readiness.scm_blockers or pending)):
        state = "not_ready"
    elif not fresh:
        state = "unknown"
    elif readiness.scm_conditions == 0:
        state = "unknown"
    elif sync_state not in {"", "synced"}:
        state = "unknown"
    else:
        state = "ready"
    return {
        "state": state,
        "connected": connected,
        "provider": provider,
        "sync_state": sync_state,
        "failed_checks": failed,
        "pending_checks": pending,
        "readiness_state": readiness.state,
        "evaluated_at": readiness.evaluated_at,
    }


__all__ = [
    "AUTHOR_REASON_KINDS",
    "CHECK_REASON_KINDS",
    "DECISION_REASON_KINDS",
    "READINESS_BLOCKER_WIRE_LIMIT",
    "READINESS_SUMMARY_KIND",
    "READINESS_SUMMARY_SCOPE",
    "READINESS_SUMMARY_VERSION",
    "REVIEWER_REASON_KINDS",
    "WORKFLOW_LABELS",
    "WORKFLOW_STATES",
    "ReadinessFact",
    "ReadinessState",
    "WorkflowDecision",
    "WorkflowFacts",
    "WorkflowState",
    "derive_workflow_state",
    "persist_readiness_summary",
    "readiness_fact",
    "readiness_scope",
    "readiness_summaries",
    "readiness_summary_payload",
    "scm_axis",
]
