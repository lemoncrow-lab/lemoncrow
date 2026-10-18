"""Canonical review priority: one queue for brief, sidebar, file order and Next.

Spec: docs/planning/2026-09-28-evidence-backed-review-design.md §7.

Tier membership comes only from records LemonCrow can point to: human
request-change state, re-review state, feedback anchor state, recorded check
results on the target's file, plan questions built from author guidance and
linked evidence, and parser proofs for Routine. Ranker descriptors -- churn,
caller counts, centrality, "public contract changed" -- are context. They may
order work inside a tier through the legacy scalar rank; they never establish
a tier.

This module is pure. It never reads a store and never writes a mark.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.routine import RoutineProof
    from lemoncrow.pro.capabilities.review.targets import AttentionLevel, ReviewTarget

PRIORITY_VERSION = 1

PriorityAttention = Literal["review_first", "understand", "routine", "done"]

TIER_POLICY_BLOCKER = 1
TIER_RE_REVIEW = 2
TIER_REVIEW_FIRST = 3
TIER_UNDERSTAND = 4
TIER_ROUTINE = 5
TIER_DONE = 6

_REASON_TIER: dict[str, int] = {
    "request_change_open": TIER_POLICY_BLOCKER,
    "decision_scope_changed": TIER_RE_REVIEW,
    "changed_since_judgment": TIER_RE_REVIEW,
    "feedback_addressed": TIER_RE_REVIEW,
    "decision_implementation_changed": TIER_RE_REVIEW,
    "decision_open": TIER_REVIEW_FIRST,
    "decision_deferred_trigger_fired": TIER_REVIEW_FIRST,
    "check_failed_linked": TIER_REVIEW_FIRST,
    "comment_orphaned": TIER_REVIEW_FIRST,
    "author_guidance_conflict": TIER_REVIEW_FIRST,
    "claim_evidence_scope_mismatch": TIER_REVIEW_FIRST,
    # AI-raised questions (review/synthesis.py) never exceed tier 3.
    "author_observation_mismatch": TIER_REVIEW_FIRST,
    "implementation_choice_unrecorded": TIER_REVIEW_FIRST,
    "fingerprint_unavailable": TIER_UNDERSTAND,
    "check_unsettled": TIER_UNDERSTAND,
    "comment_open": TIER_UNDERSTAND,
    "unreviewed": TIER_UNDERSTAND,
    # Generated/vendor is context, never permission to hide (spec §4.4).
    "generated_or_vendor": TIER_UNDERSTAND,
    "routine_comment_only": TIER_ROUTINE,
    "routine_formatting_only": TIER_ROUTINE,
    "reviewed": TIER_DONE,
    # Phase 9 policy reasons (review/criticality.py). Trusted-base or operator
    # policy and unresolved current P0/P1 findings promote outstanding work to
    # Review first; they never reopen a judgment.
    "review_first_policy": TIER_REVIEW_FIRST,
    "criticality_policy": TIER_REVIEW_FIRST,
    "finding_unresolved": TIER_REVIEW_FIRST,
    # ``priority: last`` never sets a tier (it is placed at TIER_DONE so it can
    # never lower one); it only sorts a target to the end of its own tier.
    "review_last_policy": TIER_DONE,
}


# Spec §7 re-review order: decisions needing reconsideration, then feedback and
# content changed since judgment, then accepted decisions whose body changed.
_RE_REVIEW_ORDER: dict[str, int] = {"decision_scope_changed": 0, "decision_implementation_changed": 2}

# Plan §3.15 canonical merge inside Review first: explicit ``priority: first``
# paths, then critical/high criticality, unresolved P0/P1 findings, failed
# linked verification, reopened human decisions, then every other reason.
_REVIEW_FIRST_CLASS: dict[str, int] = {
    "review_first_policy": 0,
    "criticality_policy": 1,
    "finding_unresolved": 2,
    "check_failed_linked": 3,
    "decision_open": 4,
    "decision_deferred_trigger_fired": 4,
}
REVIEW_FIRST_OTHER_CLASS = 5


def queue_class(code: str) -> int:
    """Sub-order of one reason code inside its tier (lower sorts earlier)."""

    tier = _REASON_TIER.get(code, TIER_UNDERSTAND)
    if tier == TIER_RE_REVIEW:
        return _RE_REVIEW_ORDER.get(code, 1)
    if tier == TIER_REVIEW_FIRST:
        return _REVIEW_FIRST_CLASS.get(code, REVIEW_FIRST_OTHER_CLASS)
    return 0


def _sub_order(tier: int, reasons: Sequence[QueueReason]) -> int:
    if tier not in (TIER_RE_REVIEW, TIER_REVIEW_FIRST):
        return 0
    return min(queue_class(reason.code) for reason in reasons if _REASON_TIER[reason.code] == tier)


def _primary_first(reasons: Sequence[QueueReason]) -> tuple[QueueReason, ...]:
    """Order *reasons* so the first is the one that placed the target.

    Every client shows ``reasons[0]`` as the target's reason. A plan question
    appended after "Not reviewed yet" sets the tier, so without this the shown
    reason and the queue position disagree. Stable within a tier.
    """

    return tuple(sorted(reasons, key=lambda reason: (_REASON_TIER[reason.code], queue_class(reason.code))))


@dataclass(frozen=True)
class QueueReason:
    """One typed reason; ``detail`` is display text clients must not parse."""

    code: str
    detail: str


@dataclass(frozen=True)
class TargetPriority:
    tier: int = TIER_UNDERSTAND
    attention: PriorityAttention = "understand"
    outstanding: bool = True
    position: int = 0
    reasons: tuple[QueueReason, ...] = ()


def attention_for_tier(tier: int) -> PriorityAttention:
    if tier <= TIER_REVIEW_FIRST:
        return "review_first"
    if tier == TIER_UNDERSTAND:
        return "understand"
    if tier == TIER_ROUTINE:
        return "routine"
    return "done"


def legacy_group_for_tier(tier: int) -> str:
    """The legacy left-pane group a file lands in, from its first outstanding tier."""

    if tier in (TIER_POLICY_BLOCKER, TIER_REVIEW_FIRST):
        return "needs_attention"
    if tier == TIER_RE_REVIEW:
        return "changed_since_my_review"
    if tier == TIER_UNDERSTAND:
        return "unreviewed"
    if tier == TIER_ROUTINE:
        return "mechanical"
    return "reviewed"


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _reasons(
    target: ReviewTarget,
    *,
    request_change_open: bool,
    proof: RoutineProof | None = None,
    questions: Sequence[QueueReason] = (),
    policy: Sequence[QueueReason] = (),
) -> tuple[QueueReason, ...]:
    counts = target.annotation_counts
    requested = request_change_open or target.state == "needs_changes"
    out: list[QueueReason] = []
    if requested:
        out.append(QueueReason("request_change_open", "Requested change is still open"))
    if target.state == "changed_since_review" or target.changed_since_mark:
        out.append(QueueReason("changed_since_judgment", "Changed since your review"))
    if counts.addressed_needs_rereview > 0:
        n = counts.addressed_needs_rereview
        verb = "needs" if n == 1 else "need"
        label = _count(n, "addressed comment", "addressed comments")
        out.append(QueueReason("feedback_addressed", f"{label} {verb} re-review"))
    if target.verification.fail_count > 0:
        label = _count(target.verification.fail_count, "check", "checks")
        out.append(QueueReason("check_failed_linked", f"{label} failing on this file"))
    if counts.orphaned > 0:
        n = counts.orphaned
        label = _count(n, "comment", "comments")
        out.append(QueueReason("comment_orphaned", f"{label} lost {'its' if n == 1 else 'their'} anchor"))
    if target.state == "unknown":
        out.append(QueueReason("fingerprint_unavailable", "Content identity unavailable; review individually"))
    if target.verification.unknown_count > 0:
        label = _count(target.verification.unknown_count, "check result", "check results")
        out.append(QueueReason("check_unsettled", f"{label} not settled"))
    # An addressed thread is already re-review work, and an open request-change
    # is already the blocker; neither is a second, plain open comment.
    plain_open = counts.open - counts.addressed_needs_rereview
    if plain_open > 0 and not requested:
        out.append(QueueReason("comment_open", _count(plain_open, "open comment", "open comments")))
    if target.state == "unreviewed":
        if proof is not None:
            out.append(QueueReason(f"routine_{proof.rule}", proof.detail))
        else:
            out.append(QueueReason("unreviewed", "Not reviewed yet"))
            if target.attention_level == "mechanical":
                out.append(QueueReason("generated_or_vendor", "Generated or vendored; context only"))
    if not out:
        return (QueueReason("reviewed", "Reviewed; content unchanged"),)
    # Plan questions and path policy sharpen outstanding work; they never
    # reopen a judgment.
    for question in (*questions, *policy):
        if question not in out:
            out.append(question)
    return tuple(out)


def _legacy_level(tier: int) -> AttentionLevel:
    if tier <= TIER_REVIEW_FIRST:
        return "high"
    if tier == TIER_ROUTINE:
        return "mechanical"
    return "normal"


# A target no author item owns sorts after every narrated one in its tier.
_UNNARRATED = 1 << 30


def assign_review_priority(
    targets: Sequence[ReviewTarget],
    *,
    request_change_target_ids: Collection[str] = (),
    unattributed_request_change_paths: Collection[str] = (),
    routine_proofs: Mapping[str, RoutineProof] | None = None,
    question_reasons: Mapping[str, Sequence[QueueReason]] | None = None,
    narrative: Mapping[str, int] | None = None,
    policy_reasons: Mapping[str, Sequence[QueueReason]] | None = None,
) -> tuple[ReviewTarget, ...]:
    """Return *targets* in canonical order, each carrying its ``priority``.

    A file-level request-change that no target owns is attached to exactly one
    target -- the file's first in source order -- so the objection is one
    action, never zero and never duplicated across siblings.

    Routine (tier 5) requires a ``routine.py`` proof; generated/vendor alone is
    Understand. ``question_reasons`` (plan questions such as an author
    conflict) promote outstanding targets to Review first. ``narrative`` maps
    a target to its owning author item's narrative position: within a tier it
    orders ahead of the legacy rank, which only breaks ties.

    ``policy_reasons`` (``criticality.target_policy_reasons`` plus unresolved
    current P0/P1 findings) attach to outstanding targets only. Inside Review
    first they order by the plan §3.15 class (``first`` paths, critical/high,
    P0/P1 findings, failed linked checks, reopened decisions, the rest); a
    ``review_last_policy`` target sorts to the end of its own tier and never
    crosses a tier boundary.

    ``attention_level`` is rewritten for legacy readers: tiers 1-3 are
    ``high``, tier 5 is ``mechanical``, everything else ``normal``.
    """

    proofs = routine_proofs or {}
    questions = question_reasons or {}
    policies = policy_reasons or {}
    story = narrative or {}
    requested = set(request_change_target_ids)
    first_by_path: dict[str, ReviewTarget] = {}
    for target in targets:
        if target.path not in unattributed_request_change_paths:
            continue
        current = first_by_path.get(target.path)
        if current is None or (target.start_line, target.unit_key) < (current.start_line, current.unit_key):
            first_by_path[target.path] = target
    requested.update(target.target_id for target in first_by_path.values())

    staged: list[tuple[tuple[int, int, int, int, int, int, str, int, str], ReviewTarget, TargetPriority]] = []
    for target in targets:
        # ``attention_level`` may already carry a previous assignment's "high";
        # the structural level is what derive_review_targets classified, so
        # reassigning is idempotent.
        base = replace(target, attention_level=target.structural_level)
        reasons = _primary_first(
            _reasons(
                base,
                request_change_open=target.target_id in requested,
                proof=proofs.get(target.target_id),
                questions=questions.get(target.target_id, ()),
                policy=policies.get(target.target_id, ()),
            )
        )
        tier = min(_REASON_TIER[reason.code] for reason in reasons)
        rank = target.attention_rank
        key = (
            tier,
            1 if any(reason.code == "review_last_policy" for reason in reasons) else 0,
            _sub_order(tier, reasons),
            story.get(target.target_id, _UNNARRATED),
            1 if rank <= 0 else 0,
            rank,
            target.path,
            target.start_line,
            target.unit_key,
        )
        priority = TargetPriority(
            tier=tier,
            attention=attention_for_tier(tier),
            outstanding=tier < TIER_DONE,
            reasons=reasons,
        )
        staged.append((key, base, priority))
    staged.sort(key=lambda row: row[0])
    return tuple(
        replace(
            base,
            attention_level=_legacy_level(priority.tier),
            priority=replace(priority, position=index),
        )
        for index, (_, base, priority) in enumerate(staged)
    )


def reprioritize(
    targets: Sequence[ReviewTarget],
    *,
    routine_proofs: Mapping[str, RoutineProof] | None = None,
    question_reasons: Mapping[str, Sequence[QueueReason]] | None = None,
    narrative: Mapping[str, int] | None = None,
    policy_reasons: Mapping[str, Sequence[QueueReason]] | None = None,
) -> tuple[ReviewTarget, ...]:
    """Re-rank already-prioritized targets with proofs, plan questions, narrative and policy.

    Request-change blockers (including a file objection already attributed to
    one target) are carried over from the first assignment, so the canonical
    queue gains inputs without re-deriving targets.
    """

    requested = {
        target.target_id
        for target in targets
        if any(reason.code == "request_change_open" for reason in target.priority.reasons)
    }
    return assign_review_priority(
        targets,
        request_change_target_ids=requested,
        routine_proofs=routine_proofs,
        question_reasons=question_reasons,
        narrative=narrative,
        policy_reasons=policy_reasons,
    )


def pinned_ranks(generations: Sequence[tuple[str, Mapping[str, int]]], started_at: str) -> dict[str, int]:
    """The ranks a reviewer who started at *started_at* keeps (stable slots).

    The cost-aware rule: progressive enrichment fills stable slots and cannot
    silently reshuffle an in-progress human queue without new evidence and an
    explanation. A re-rank is ranking only -- it brings no evidence about any
    target -- so it never moves the outstanding items of a reviewer who had
    judged something on this Snapshot before it arrived. *generations* are
    ``(superseded_at, ranks)`` as ``ReviewStore.read_ranking_history`` keeps
    them; the result maps unit keys to the rank that was current at
    *started_at*, and is empty for a reviewer who had not started (``""``) or
    started after every re-rank. Tiers still move: a confirmed finding or a
    plan question carries its evidence and its reason.
    """

    if not started_at:
        return {}
    out: dict[str, int] = {}
    # Latest first, so the earliest generation superseded after the start wins.
    for superseded_at, ranks in sorted(generations, key=lambda row: row[0], reverse=True):
        if superseded_at > started_at:
            out.update(ranks)
    return out


def priority_payload(priority: TargetPriority) -> dict[str, Any]:
    return {
        "version": PRIORITY_VERSION,
        "tier": priority.tier,
        "attention": priority.attention,
        "outstanding": priority.outstanding,
        "position": priority.position,
        "reasons": [{"code": reason.code, "detail": reason.detail} for reason in priority.reasons],
    }


__all__ = [
    "PRIORITY_VERSION",
    "REVIEW_FIRST_OTHER_CLASS",
    "TIER_DONE",
    "TIER_POLICY_BLOCKER",
    "TIER_REVIEW_FIRST",
    "TIER_RE_REVIEW",
    "TIER_ROUTINE",
    "TIER_UNDERSTAND",
    "PriorityAttention",
    "QueueReason",
    "TargetPriority",
    "assign_review_priority",
    "attention_for_tier",
    "legacy_group_for_tier",
    "pinned_ranks",
    "priority_payload",
    "queue_class",
    "reprioritize",
]
