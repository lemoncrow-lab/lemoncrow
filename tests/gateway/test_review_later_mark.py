"""`later` ("Decide later"): set aside, never judged, still open, last in its tier.

The guard that matters is the partition test: every mark state lands in exactly one progress counter,
so a state added later cannot silently fall out of a sum and read as reviewed.
"""

from __future__ import annotations

from lemoncrow.pro.capabilities.review.answer_category import QUICK_CHECK, effective_answer
from lemoncrow.pro.capabilities.review.review_priority import (
    TIER_REVIEW_FIRST,
    TIER_UNDERSTAND,
    QueueReason,
    assign_review_priority,
)
from lemoncrow.pro.capabilities.review.routine import RoutineProof
from lemoncrow.pro.capabilities.review.session_models import JUDGED_MARK_STATES, MARK_STATES
from lemoncrow.pro.capabilities.review.targets import (
    ReviewTarget,
    build_review_outline,
    outline_payload,
    progress_payload,
    review_progress,
)


def _target(key: str, *, path: str = "src/a.py", state: str = "unreviewed", rank: int = 0) -> ReviewTarget:
    return ReviewTarget(
        target_id=f"target:{key}",
        unit_key=key,
        kind="symbol",
        path=path,
        label=f"{path}::{key}",
        symbol=key,
        start_line=1,
        end_line=1,
        hunk_ordinals=(0,),
        spans=(),
        state=state,  # type: ignore[arg-type]
        changed_since_mark=False,
        reviewed_revision_id="",
        attention_rank=rank,
        attention_level="normal",
        structural_level="normal",
        reasons=(),
        additions=1,
        deletions=0,
        fingerprint_method="symbol_body_sha256",
        structural=QUICK_CHECK,
        answer=effective_answer(QUICK_CHECK),
    )


def test_a_later_part_stays_outstanding_with_its_own_reason_and_is_not_done() -> None:
    (target,) = assign_review_priority((_target("k", state="later"),))
    assert target.priority.outstanding is True
    assert target.priority.tier == TIER_UNDERSTAND
    assert [reason.code for reason in target.priority.reasons] == ["left_for_later"]
    assert target.priority.reasons[0].detail == "You left this for later"


def test_a_later_part_goes_to_the_end_of_its_tier_and_keeps_a_review_first_tier() -> None:
    targets = (
        _target("set-aside", path="a.py", state="later", rank=1),
        _target("untouched", path="b.py", rank=2),
        _target("risky-set-aside", path="c.py", state="later", rank=3),
        _target("risky", path="d.py", rank=4),
    )
    policy = {
        "target:risky-set-aside": [QueueReason("removed_safeguard", "No longer rejects")],
        "target:risky": [QueueReason("removed_safeguard", "No longer rejects")],
    }
    ordered = assign_review_priority(targets, policy_reasons=policy)
    assert [t.unit_key for t in ordered] == ["risky", "risky-set-aside", "untouched", "set-aside"]
    by_key = {t.unit_key: t for t in ordered}
    assert by_key["risky-set-aside"].priority.tier == TIER_REVIEW_FIRST
    assert by_key["risky-set-aside"].priority.outstanding
    # Setting a risky part aside does not make it any less a decision.
    assert by_key["risky-set-aside"].answer.category == "decision"


def test_a_later_part_is_not_a_proven_routine_part() -> None:
    proof = RoutineProof("src/a.py", "comment_only", "python", "Comment-only change")
    (target,) = assign_review_priority((_target("c", state="later"),), routine_proofs={"target:c": proof})
    assert target.priority.attention == "understand"


def test_every_mark_state_is_counted_in_exactly_one_progress_counter() -> None:
    counters = ("reviewed", "changed_since_review", "needs_changes", "unreviewed", "unknown", "later")
    assert set(counters) == set(MARK_STATES) | {"reviewed"}
    for state in MARK_STATES:
        progress = progress_payload(review_progress((_target("k", state=state),)))
        counted = [name for name in counters if progress[name]]
        assert counted == [state], f"{state} must land in its own counter, got {counted}"
        outline = outline_payload(build_review_outline(assign_review_priority((_target("k", state=state),)))[0])
        assert [name for name in counters if outline[name]] == [state]


def test_later_is_never_judged_in_any_tally() -> None:
    assert "later" not in JUDGED_MARK_STATES
    assert "later" in MARK_STATES
    targets = assign_review_priority(
        (_target("a", state="reviewed"), _target("b", state="later"), _target("c", state="later"))
    )
    progress = review_progress(targets)
    assert (progress.reviewed, progress.later, progress.needs_changes) == (1, 2, 0)
    assert progress.reviewed + progress.needs_changes < progress.target_count
    assert sum(target.priority.outstanding for target in targets) == 2


def test_the_frontend_vocabulary_is_the_servers() -> None:
    """The Reader restates the mark states and categories as types; a state the server adds must reach it."""
    import re
    from pathlib import Path

    from lemoncrow.pro.capabilities.review.answer_category import ACCEPTED_BY_RULE_RULES
    from lemoncrow.pro.capabilities.review.session_models import ANSWER_CATEGORIES

    root = Path(__file__).resolve().parents[2] / "frontend" / "src" / "review"
    types = (root / "types.ts").read_text(encoding="utf-8")
    model = (root / "answerModel.ts").read_text(encoding="utf-8")

    def quoted(block: str) -> list[str]:
        return re.findall(r'"([a-z_]+)"', block)

    union = re.search(r"export type MarkState =(.*?);", types, re.S)
    assert union is not None
    assert quoted(union.group(1)) == list(MARK_STATES)
    states = re.search(r"export const MARK_STATES = \[(.*?)\] as const", model, re.S)
    assert states is not None
    assert quoted(states.group(1)) == list(MARK_STATES)
    category = re.search(r"export type AnswerCategory =(.*?);", types, re.S)
    assert category is not None
    assert quoted(category.group(1)) == list(ANSWER_CATEGORIES)
    rules = re.search(r"const RULES = \[(.*?)\] as const", model, re.S)
    assert rules is not None
    assert quoted(rules.group(1)) == list(ACCEPTED_BY_RULE_RULES)


def test_a_surface_that_cannot_derive_the_queue_prints_no_category_counts() -> None:
    """The CLI's own target derivation has no policy facts, so it must not print a category the Reader would not."""
    counts = progress_payload(review_progress((_target("a"), _target("b", state="later"))), answers=False)
    assert "later" in counts and counts["later"] == 1
    assert not {"decision", "quick_check", "accepted_by_rule"} & set(counts)
    assert {"decision", "quick_check", "accepted_by_rule"} <= set(progress_payload(review_progress((_target("a"),))))


def test_a_risky_part_is_a_decision_whatever_the_reviewer_has_marked() -> None:
    """The answer reads the facts as if nobody had judged the part, so a mark can never lower it."""
    risky = {"target:k": [QueueReason("removed_safeguard", "No longer rejects a percent over 100")]}
    answers = {}
    for state in MARK_STATES:
        (target,) = assign_review_priority((_target("k", state=state),), policy_reasons=risky)
        answers[state] = target.answer.category
    assert set(answers.values()) == {"decision"}, answers


def test_the_legacy_session_score_does_not_prefer_a_review_for_its_later_marks(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.review.session_models import ReviewMark, ReviewRevision, ReviewSession, ReviewUnit
    from lemoncrow.pro.capabilities.review.sources.local import _legacy_session_score
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    store = ReviewStore(tmp_path / "store")
    session = store.create_session(
        ReviewSession(id="", subject_type="local_change", repo_root="/r", range_mode="working_tree", title="t")
    )
    revision = store.add_revision(
        ReviewRevision(
            id="",
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="t",
            packet_schema_version=1,
        ),
        [ReviewUnit(revision_id="", unit_key="fil:a", kind="file", path="a", content_fingerprint="fp")],
    )

    def score(state: str) -> int:
        store.set_mark(
            ReviewMark(
                review_id=session.id,
                unit_key="fil:a",
                state=state,
                reviewed_revision_id=revision.id,  # type: ignore[arg-type]
                content_fingerprint="fp",
            )
        )
        return _legacy_session_score(store, session)[2]

    assert score("later") == 0  # substantive judgment marks: none
    assert score("reviewed") == 1


def test_a_stray_later_mark_on_a_behavior_target_is_simply_not_judged() -> None:
    from lemoncrow.pro.capabilities.review.behavior import _judged_state
    from lemoncrow.pro.capabilities.review.session_models import ReviewMark

    later = ReviewMark(
        review_id="r", unit_key="beh:x", state="later", reviewed_revision_id="rr", content_fingerprint="old"
    )
    assert _judged_state("old", later) == ("unreviewed", False, "")
    assert _judged_state("new", later) == ("unreviewed", False, "")
    reviewed = ReviewMark(
        review_id="r", unit_key="beh:x", state="reviewed", reviewed_revision_id="rr", content_fingerprint="old"
    )
    assert _judged_state("old", reviewed)[0] == "reviewed"


def test_a_store_that_cannot_record_later_refuses_it_at_the_one_door_every_mark_goes_through(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import pytest

    from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession, ReviewUnit
    from lemoncrow.pro.capabilities.review.sources.local import LaterMarkUnavailable, mark_unit
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    class _HostedLike(ReviewStore):
        def later_mark_supported(self) -> bool:
            return False

    store = _HostedLike(tmp_path / "store")
    session = store.create_session(
        ReviewSession(id="", subject_type="local_change", repo_root="/r", range_mode="working_tree", title="t")
    )
    revision = store.add_revision(
        ReviewRevision(
            id="",
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="t",
            packet_schema_version=1,
        ),
        [ReviewUnit(revision_id="", unit_key="fil:a", kind="file", path="a", content_fingerprint="fp")],
    )
    unit = store.list_units(revision.id)[0]
    with pytest.raises(LaterMarkUnavailable, match="not available on hosted reviews yet"):
        mark_unit(store, session, revision, unit, state="later")
    assert store.list_marks(session.id) == ()
    assert mark_unit(store, session, revision, unit, state="needs_changes").state == "needs_changes"
    assert ReviewStore(tmp_path / "other").later_mark_supported() is True
