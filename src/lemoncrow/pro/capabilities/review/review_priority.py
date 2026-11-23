"""Canonical review priority: one queue for brief, sidebar, file order and Next.

Spec: docs/planning/2026-09-28-evidence-backed-review-design.md §7.

Tier membership comes only from records LemonCrow can point to: human
request-change state, re-review state, feedback anchor state, recorded check
results on the target's file, plan questions built from author guidance and
linked evidence, parser proofs for Routine, and the listed outside files that
still use a changed (not added) symbol (``wide_reach``), and a safeguard the
diff removes or narrows (``removed_safeguard``). Ranker descriptors --
churn, caller counts, centrality, "public contract changed" -- are context.
They may order work inside a tier through the legacy scalar rank; they never
establish a tier.

This module is pure. It never reads a store and never writes a mark.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from lemoncrow.pro.capabilities.review.answer_category import effective_answer

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.answer_category import Answer
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
    # A changed symbol that files outside the change still call: its effect
    # reaches code the diff does not show.
    "wide_reach": TIER_REVIEW_FIRST,
    # A changed part whose diff removes or narrows a raise, assert or early
    # return (review/change_risk.py): a High risk fact, read from the diff.
    "removed_safeguard": TIER_REVIEW_FIRST,
    "fingerprint_unavailable": TIER_UNDERSTAND,
    "check_unsettled": TIER_UNDERSTAND,
    "comment_open": TIER_UNDERSTAND,
    "unreviewed": TIER_UNDERSTAND,
    # "Decide later": the reviewer set the part aside. It is not a judgment, so it stays outstanding.
    "left_for_later": TIER_UNDERSTAND,
    # Generated/vendor is context, never permission to hide (spec §4.4).
    "generated_or_vendor": TIER_UNDERSTAND,
    "routine_comment_only": TIER_ROUTINE,
    "routine_formatting_only": TIER_ROUTINE,
    "reviewed": TIER_DONE,
    # Phase 9 policy reasons (review/criticality.py). Trusted-base or operator
    # policy and unresolved current P0/P1 findings promote outstanding work to
    # Review first; they never reopen a judgment.
    "review_first_policy": TIER_REVIEW_FIRST,
    "sensitive_path_attention": TIER_REVIEW_FIRST,
    "structural_decision_question": TIER_REVIEW_FIRST,
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
    "sensitive_path_attention": 5,
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
    #: The target's own reasons (risk facts, policy, plan questions, failed or orphaned evidence) put it in
    #: Review first whenever it is outstanding. A judgment never clears it, so the Brief's Understanding
    #: keeps counting a part after it is marked; the reviewer's own objection or a re-review never sets it.
    review_first: bool = False


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
    if target.state == "later":
        out.append(QueueReason("left_for_later", "You left this for later"))
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
    ai_reasons: Mapping[str, Sequence[QueueReason]] | None = None,
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

    ``ai_reasons`` names the reasons in ``question_reasons`` / ``policy_reasons`` that an AI raised (AI
    questions, AI findings). They order and tier the queue as before, but the part's answer category
    ignores them: AI can never raise or lower how a part is asked of a person.

    ``attention_level`` is rewritten for legacy readers: tiers 1-3 are
    ``high``, tier 5 is ``mechanical``, everything else ``normal``.
    """

    proofs = routine_proofs or {}
    questions = question_reasons or {}
    policies = policy_reasons or {}
    from_ai = ai_reasons or {}
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

    staged: list[
        tuple[tuple[int, int, int, int, int, int, int, str, int, str], ReviewTarget, TargetPriority, Answer]
    ] = []
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
        # The same reasons on the part as if nobody had judged it yet: Review first membership, not position.
        unjudged = _reasons(
            replace(base, state="unreviewed", changed_since_mark=False),
            request_change_open=False,
            questions=questions.get(target.target_id, ()),
            policy=policies.get(target.target_id, ()),
        )
        review_first = any(_REASON_TIER[reason.code] == TIER_REVIEW_FIRST for reason in unjudged)
        # The answer reads the same facts without what an AI raised, so it is the same with AI on or off.
        ai_here = from_ai.get(target.target_id, ())
        if ai_here:
            checked = _reasons(
                replace(base, state="unreviewed", changed_since_mark=False),
                request_change_open=False,
                questions=[r for r in questions.get(target.target_id, ()) if r not in ai_here],
                policy=[r for r in policies.get(target.target_id, ()) if r not in ai_here],
            )
            answer_review_first = any(_REASON_TIER[reason.code] == TIER_REVIEW_FIRST for reason in checked)
        else:
            answer_review_first = review_first
        rank = target.attention_rank
        key = (
            tier,
            # A part the reviewer left for later goes to the end of its tier, as does ``priority: last``.
            1 if target.state == "later" or any(reason.code == "review_last_policy" for reason in reasons) else 0,
            _sub_order(tier, reasons),
            # Inside one class, more independent reasons to look first sort earlier: an auth path that
            # outside files also call comes before code that outside files only call.
            -len({reason.code for reason in reasons if _REASON_TIER[reason.code] == tier == TIER_REVIEW_FIRST}),
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
            review_first=review_first,
        )
        proof = proofs.get(target.target_id)
        answer = effective_answer(
            target.structural,
            review_first=answer_review_first,
            routine_rule=proof.rule if proof is not None else "",
        )
        staged.append((key, base, priority, answer))
    staged.sort(key=lambda row: row[0])
    return tuple(
        replace(
            base,
            attention_level=_legacy_level(priority.tier),
            priority=replace(priority, position=index),
            answer=answer,
        )
        for index, (_, base, priority, answer) in enumerate(staged)
    )


def reprioritize(
    targets: Sequence[ReviewTarget],
    *,
    routine_proofs: Mapping[str, RoutineProof] | None = None,
    question_reasons: Mapping[str, Sequence[QueueReason]] | None = None,
    narrative: Mapping[str, int] | None = None,
    policy_reasons: Mapping[str, Sequence[QueueReason]] | None = None,
    ai_reasons: Mapping[str, Sequence[QueueReason]] | None = None,
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
        ai_reasons=ai_reasons,
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


class OutsideReach(Protocol):
    """``consumer_inventory.OutsideReach``, structurally: target derivation must not import the inventory."""

    @property
    def change(self) -> str: ...
    @property
    def files(self) -> tuple[str, ...]: ...
    @property
    def calls_only(self) -> bool: ...
    @property
    def at_least(self) -> bool: ...


def reach_phrase(reach: OutsideReach) -> str:
    """``called from 3 files outside this change``; ``""`` when no non-test file uses the symbol."""

    if not reach.files:
        return ""
    verb = "called from" if reach.calls_only else "used in"
    count = f"at least {len(reach.files)}" if reach.at_least else str(len(reach.files))
    return f"{verb} {count} {'file' if len(reach.files) == 1 else 'files'} outside this change"


def wide_reach_reasons(reach: Mapping[str, OutsideReach]) -> dict[str, list[QueueReason]]:
    """``wide_reach`` per target whose modified or deleted symbol outside non-test files still use.

    An added symbol is excluded: nothing outside the change can depend on its
    old behavior. Test files alone do not widen reach.
    """

    out: dict[str, list[QueueReason]] = {}
    for target_id, row in reach.items():
        phrase = reach_phrase(row)
        if phrase and row.change != "added":
            out[target_id] = [QueueReason("wide_reach", phrase[0].upper() + phrase[1:])]
    return out


def safeguard_reasons(found: Mapping[str, Sequence[str]]) -> dict[str, list[QueueReason]]:
    """``removed_safeguard`` per target from ``change_risk.removed_safeguards`` phrases.

    The reason is the first phrase in plain words, like every other reason:
    ``No longer rejects a percent over 100 (check narrowed from percent < 0 or
    percent > 100 to percent < 0)``.
    """

    out: dict[str, list[QueueReason]] = {}
    for target_id, phrases in found.items():
        if phrases:
            text = phrases[0].replace("`", "")
            out[target_id] = [QueueReason("removed_safeguard", text[:1].upper() + text[1:])]
    return out


def priority_payload(priority: TargetPriority) -> dict[str, Any]:
    return {
        "version": PRIORITY_VERSION,
        "tier": priority.tier,
        "attention": priority.attention,
        "outstanding": priority.outstanding,
        "position": priority.position,
        "reasons": [{"code": reason.code, "detail": reason.detail} for reason in priority.reasons],
        "review_first": priority.review_first,
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
    "reach_phrase",
    "reprioritize",
    "safeguard_reasons",
    "wide_reach_reasons",
]
