"""Spec §7: one canonical queue; tiers come only from sourced records."""

from __future__ import annotations

from dataclasses import replace

from lemoncrow.pro.capabilities.review.consumer_inventory import OutsideReach
from lemoncrow.pro.capabilities.review.review_priority import (
    TIER_DONE,
    TIER_POLICY_BLOCKER,
    TIER_RE_REVIEW,
    TIER_REVIEW_FIRST,
    TIER_ROUTINE,
    TIER_UNDERSTAND,
    QueueReason,
    assign_review_priority,
    legacy_group_for_tier,
    reach_phrase,
    safeguard_reasons,
    wide_reach_reasons,
)
from lemoncrow.pro.capabilities.review.routine import RoutineProof
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, TargetAnnotationCounts, TargetVerification


def _target(
    key: str,
    *,
    path: str = "src/a.py",
    start: int = 1,
    state: str = "unreviewed",
    changed: bool = False,
    rank: int = 0,
    level: str = "normal",
    reasons: tuple[str, ...] = (),
    fail: int = 0,
    unknown: int = 0,
    open_: int = 0,
    orphaned: int = 0,
    addressed: int = 0,
) -> ReviewTarget:
    return ReviewTarget(
        target_id=f"target:{key}",
        unit_key=key,
        kind="symbol",
        path=path,
        label=f"{path}::{key}",
        symbol=key,
        start_line=start,
        end_line=start,
        hunk_ordinals=(0,),
        spans=(),
        state=state,
        changed_since_mark=changed,
        reviewed_revision_id="",
        attention_rank=rank,
        attention_level=level,
        structural_level=level,
        reasons=reasons,
        additions=1,
        deletions=0,
        fingerprint_method="symbol_body_sha256",
        verification=TargetVerification(fail_count=fail, unknown_count=unknown),
        annotation_counts=TargetAnnotationCounts(open=open_, orphaned=orphaned, addressed_needs_rereview=addressed),
    )


def _codes(target: ReviewTarget) -> list[str]:
    return [reason.code for reason in target.priority.reasons]


def test_review_priority_tier_contract() -> None:
    """case: test_the_author_narrative_orders_a_tier_before_the_legacy_rank"""
    "Spec §7: within a tier, narrative order first; the old rank only breaks ties."
    targets = (
        _target("ranked-first", path="a.py", rank=1),
        _target("ranked-second", path="b.py", rank=2),
        _target("ranked-third", path="c.py", rank=3),
        _target("blocker", path="d.py", state="needs_changes", rank=9),
    )
    narrative = {"target:ranked-third": 0, "target:ranked-second": 1}
    ordered = assign_review_priority(targets, narrative=narrative)
    assert [t.unit_key for t in ordered] == ["blocker", "ranked-third", "ranked-second", "ranked-first"]
    assert [t.unit_key for t in assign_review_priority(targets)] == [
        "blocker",
        "ranked-first",
        "ranked-second",
        "ranked-third",
    ]
    "case: test_tiers_follow_sourced_records_not_ranker_descriptors"
    targets = (
        _target("plain", path="a.py", rank=9),
        _target("contract", path="b.py", rank=1, reasons=("public contract changed", "4 known callers")),
        _target("changed", path="c.py", state="changed_since_review", changed=True, rank=5),
        _target("asked", path="d.py", state="needs_changes", rank=7),
        _target("failing", path="e.py", state="reviewed", fail=1, rank=8),
        _target("done", path="f.py", state="reviewed", rank=2),
        _target("vendored", path="vendor/g.js", level="mechanical", rank=3),
    )
    ordered = assign_review_priority(targets)
    assert [t.unit_key for t in ordered] == ["asked", "changed", "failing", "contract", "vendored", "plain", "done"]
    tiers = {t.unit_key: t.priority.tier for t in ordered}
    assert tiers == {
        "asked": TIER_POLICY_BLOCKER,
        "changed": TIER_RE_REVIEW,
        "failing": TIER_REVIEW_FIRST,
        "contract": TIER_UNDERSTAND,
        "plain": TIER_UNDERSTAND,
        "vendored": TIER_UNDERSTAND,
        "done": TIER_DONE,
    }
    assert [t.priority.position for t in ordered] == list(range(len(targets)))
    assert {t.unit_key: t.attention_level for t in ordered} == {
        "asked": "high",
        "changed": "high",
        "failing": "high",
        "contract": "normal",
        "plain": "normal",
        "vendored": "normal",
        "done": "normal",
    }
    "case: test_generated_or_vendored_without_a_proof_is_understand_with_context"
    (target,) = assign_review_priority((_target("v", path="vendor/g.js", level="mechanical"),))
    assert target.priority.tier == TIER_UNDERSTAND
    assert _codes(target) == ["unreviewed", "generated_or_vendor"]
    assert target.attention_level == "normal"
    "case: test_only_a_parser_proof_makes_a_target_routine"
    (target,) = assign_review_priority((_target("x"),), routine_proofs={"target:x": _PROOF})
    assert target.priority.tier == TIER_ROUTINE
    assert target.priority.attention == "routine"
    assert _codes(target) == ["routine_comment_only"]
    assert target.priority.reasons[0].detail == _PROOF.detail
    assert target.attention_level == "mechanical"
    "case: test_a_proof_never_hides_human_state"
    proofs = {f"target:{key}": _PROOF for key in ("asked", "changed", "done")}
    ordered = assign_review_priority(
        (
            _target("asked", state="needs_changes"),
            _target("changed", state="changed_since_review", changed=True),
            _target("done", state="reviewed"),
        ),
        routine_proofs=proofs,
    )
    assert {t.unit_key: t.priority.tier for t in ordered} == {
        "asked": TIER_POLICY_BLOCKER,
        "changed": TIER_RE_REVIEW,
        "done": TIER_DONE,
    }


def test_review_priority_reason_contract() -> None:
    """case: test_question_reasons_promote_outstanding_targets_to_review_first"""
    question = QueueReason("claim_evidence_scope_mismatch", "legacy migration: existing reviews not exercised")
    ordered = assign_review_priority(
        (_target("x"), _target("proven"), _target("done", state="reviewed")),
        routine_proofs={"target:proven": _PROOF},
        question_reasons={key: [question] for key in ("target:x", "target:proven", "target:done")},
    )
    by_key = {t.unit_key: t for t in ordered}
    assert by_key["x"].priority.tier == TIER_REVIEW_FIRST
    assert "claim_evidence_scope_mismatch" in _codes(by_key["x"])
    assert by_key["x"].attention_level == "high"
    assert by_key["proven"].priority.tier == TIER_REVIEW_FIRST
    assert by_key["done"].priority.tier == TIER_DONE
    "case: test_first_reason_is_the_one_that_placed_the_target"
    question = QueueReason("author_guidance_conflict", "Authors disagree about retention")
    scope = QueueReason("decision_scope_changed", "Decision: scope changed")
    ordered = assign_review_priority(
        (
            _target("asked", path="a.py", open_=1),
            _target("changed", path="b.py", state="changed_since_review", changed=True),
        ),
        question_reasons={"target:asked": [question], "target:changed": [scope]},
    )
    by_key = {t.unit_key: t for t in ordered}
    asked = by_key["asked"].priority
    assert asked.tier == TIER_REVIEW_FIRST
    assert asked.reasons[0] == question
    assert _codes(by_key["asked"]) == ["author_guidance_conflict", "comment_open", "unreviewed"]
    changed = by_key["changed"].priority
    assert changed.tier == TIER_RE_REVIEW
    assert [reason.code for reason in changed.reasons] == ["decision_scope_changed", "changed_since_judgment"]
    "case: test_reprioritize_keeps_every_request_change_blocker"
    from lemoncrow.pro.capabilities.review.review_priority import reprioritize

    first = assign_review_priority(
        (_target("a", path="src/x.py", start=1), _target("b", path="src/x.py", start=9), _target("c")),
        unattributed_request_change_paths={"src/x.py"},
    )
    again = reprioritize(first, routine_proofs={"target:c": _PROOF})
    tiers = {t.unit_key: t.priority.tier for t in again}
    assert tiers == {"a": TIER_POLICY_BLOCKER, "b": TIER_UNDERSTAND, "c": TIER_ROUTINE}
    "case: test_reassigning_with_proofs_and_questions_is_idempotent"
    question = QueueReason("author_guidance_conflict", "Authors disagree about checkpoint-retention")
    kwargs = {"routine_proofs": {"target:p": _PROOF}, "question_reasons": {"target:q": [question]}}
    once = assign_review_priority((_target("p"), _target("q"), _target("r")), **kwargs)
    assert assign_review_priority(once, **kwargs) == once
    "case: test_a_caller_count_alone_never_leaves_understand"
    ordered = assign_review_priority((_target("x", reasons=("12 known callers",), rank=1),))
    assert ordered[0].priority.tier == TIER_UNDERSTAND
    assert ordered[0].priority.attention == "understand"
    assert _codes(ordered[0]) == ["unreviewed"]


def test_review_priority_outstanding_contract() -> None:
    """case: test_reviewed_target_with_open_feedback_or_unsettled_check_is_still_outstanding"""
    ordered = assign_review_priority(
        (
            _target("commented", path="a.py", state="reviewed", open_=1),
            _target("unsettled", path="b.py", state="reviewed", unknown=2),
            _target("lost", path="c.py", state="reviewed", orphaned=1),
            _target("answered", path="d.py", state="reviewed", open_=1, addressed=1),
        )
    )
    by_key = {t.unit_key: t for t in ordered}
    assert all(t.priority.outstanding for t in ordered)
    assert _codes(by_key["commented"]) == ["comment_open"]
    assert _codes(by_key["unsettled"]) == ["check_unsettled"]
    assert by_key["lost"].priority.tier == TIER_REVIEW_FIRST
    assert _codes(by_key["answered"]) == ["feedback_addressed"]
    assert by_key["answered"].priority.tier == TIER_RE_REVIEW
    "case: test_request_change_ids_and_unattributed_file_objection_each_create_one_blocker"
    targets = (
        _target("a1", path="a.py", start=10),
        _target("a2", path="a.py", start=2),
        _target("b1", path="b.py", start=1),
    )
    ordered = assign_review_priority(
        targets, request_change_target_ids={"target:b1"}, unattributed_request_change_paths={"a.py"}
    )
    blockers = [t.unit_key for t in ordered if t.priority.tier == TIER_POLICY_BLOCKER]
    assert blockers == ["a2", "b1"]
    assert [t.unit_key for t in ordered][-1] == "a1"
    "case: test_every_target_appears_exactly_once_with_contiguous_positions"
    targets = tuple(_target(f"k{i}", path=f"p{i % 7}.py", start=i, rank=i % 3) for i in range(300))
    ordered = assign_review_priority(targets)
    assert sorted(t.unit_key for t in ordered) == sorted(t.unit_key for t in targets)
    assert [t.priority.position for t in ordered] == list(range(300))
    "case: test_within_a_tier_rank_then_source_order_breaks_ties_and_unranked_goes_last"
    ordered = assign_review_priority(
        (
            _target("z", path="z.py", rank=0),
            _target("b2", path="b.py", start=20, rank=2),
            _target("b1", path="b.py", start=5, rank=2),
            _target("a", path="a.py", rank=1),
        )
    )
    assert [t.unit_key for t in ordered] == ["a", "b1", "b2", "z"]


def test_review_priority_idempotence_contract() -> None:
    """case: test_legacy_groups_are_a_function_of_tier"""
    assert legacy_group_for_tier(TIER_POLICY_BLOCKER) == "needs_attention"
    assert legacy_group_for_tier(TIER_RE_REVIEW) == "changed_since_my_review"
    assert legacy_group_for_tier(TIER_REVIEW_FIRST) == "needs_attention"
    assert legacy_group_for_tier(TIER_UNDERSTAND) == "unreviewed"
    assert legacy_group_for_tier(TIER_ROUTINE) == "mechanical"
    assert legacy_group_for_tier(TIER_DONE) == "reviewed"
    "case: test_reassigning_is_idempotent"
    once = assign_review_priority((_target("a", state="needs_changes"), _target("b", path="b.py")))
    twice = assign_review_priority(tuple(replace(t, attention_level="normal") for t in once))
    assert [(t.unit_key, t.priority) for t in once] == [(t.unit_key, t.priority) for t in twice]
    "case: test_re_review_sub_order_puts_reconsideration_first_and_body_changes_last"
    targets = (
        _target("body", path="a.py"),
        _target("changed", path="b.py", state="changed_since_review", changed=True),
        _target("scope", path="c.py"),
    )
    ordered = assign_review_priority(
        targets,
        question_reasons={
            "target:body": [QueueReason("decision_implementation_changed", "Decision: body changed")],
            "target:scope": [QueueReason("decision_scope_changed", "Decision: scope changed")],
        },
    )
    assert [target.unit_key for target in ordered] == ["scope", "changed", "body"]
    assert {target.priority.tier for target in ordered} == {TIER_RE_REVIEW}
    "case: test_decision_reasons_open_and_fired_deferral_are_review_first"
    ordered = assign_review_priority(
        (_target("open", path="a.py"), _target("fired", path="b.py")),
        question_reasons={
            "target:open": [QueueReason("decision_open", "Decision needed")],
            "target:fired": [QueueReason("decision_deferred_trigger_fired", "Deferred decision is due")],
        },
    )
    assert {target.priority.tier for target in ordered} == {TIER_REVIEW_FIRST}


_PROOF = RoutineProof("src/a.py", "comment_only", "python", "Comment-only change; code is byte-identical (Python)")


def _reach(target_id: str, change: str, files: tuple[str, ...], tests: tuple[str, ...] = ()) -> OutsideReach:
    return OutsideReach(
        target_id=target_id,
        path="invoices/list.ts",
        symbol="listInvoices",
        change=change,
        files=files,
        tests=tests,
        calls_only=True,
        at_least=False,
    )


def test_a_changed_symbol_called_from_outside_the_change_ranks_review_first() -> None:
    outside = ("api/v1/invoices.ts", "jobs/exportInvoices.ts", "web/Dashboard.tsx")
    reasons = wide_reach_reasons(
        {
            "target:list": _reach("target:list", "modified", outside, ("invoices/list.test.ts",)),
            # Nothing outside can depend on an added symbol's old behavior.
            "target:wrap": _reach("target:wrap", "added", ("x.ts",)),
            # Tests alone do not widen reach.
            "target:tested": _reach("target:tested", "modified", (), ("a.test.ts",)),
        }
    )
    assert reasons == {"target:list": [QueueReason("wide_reach", "Called from 3 files outside this change")]}

    targets = (_target("wrap", path="lib/cache.ts"), _target("list", path="invoices/list.ts"))
    ordered = assign_review_priority(targets, policy_reasons=reasons)
    assert [t.unit_key for t in ordered] == ["list", "wrap"]
    first = ordered[0].priority
    assert first.tier == TIER_REVIEW_FIRST and first.attention == "review_first"
    assert first.reasons[0].detail == "Called from 3 files outside this change"
    # A judgment is never reopened by reach.
    reviewed = assign_review_priority((_target("list", state="reviewed"),), policy_reasons=reasons)
    assert reviewed[0].priority.tier == TIER_DONE


def test_more_reasons_to_look_first_sort_earlier_inside_one_class() -> None:
    """An auth path that outside files call outranks code they only call, whatever the file order."""

    reach = [QueueReason("wide_reach", "Called from 1 file outside this change")]
    auth = [QueueReason("sensitive_path_attention", "Access-control code changed"), *reach]
    targets = (
        _target("order_total", path="app/orders.py", rank=1),
        _target("apply_discount", path="app/orders.py", start=8, rank=2),
        _target("can_view_order", path="app/auth.py", rank=3),
    )
    ordered = assign_review_priority(
        targets,
        policy_reasons={"target:order_total": reach, "target:apply_discount": reach, "target:can_view_order": auth},
    )
    assert [t.unit_key for t in ordered] == ["can_view_order", "order_total", "apply_discount"]
    assert ordered[0].priority.reasons[0].code == "sensitive_path_attention"


def test_reach_wording_follows_what_was_listed() -> None:
    single = replace(_reach("t", "deleted", ("a.ts",)), calls_only=False)
    assert reach_phrase(single) == "used in 1 file outside this change"
    assert reach_phrase(replace(single, at_least=True)) == "used in at least 1 file outside this change"


def test_a_removed_safeguard_ranks_review_first_with_its_fact_while_untested_parts_stay_understand() -> None:
    """The demo: apply_discount's narrowed guard joins the auth part in Review first; order_total does not."""

    phrase = (
        "no longer rejects a percent over 100 (check narrowed from `percent < 0 or percent > 100` to `percent < 0`)"
    )
    fact = "No longer rejects a percent over 100 (check narrowed from percent < 0 or percent > 100 to percent < 0)"
    policy = safeguard_reasons({"target:apply_discount": [phrase], "target:empty": []})
    assert policy == {"target:apply_discount": [QueueReason("removed_safeguard", fact)]}
    auth = [
        QueueReason("sensitive_path_attention", "Access-control code changed"),
        QueueReason("wide_reach", "Called from 1 file outside this change"),
    ]
    targets = (
        _target("order_total", path="app/orders.py", rank=1),
        _target("apply_discount", path="app/orders.py", start=8, rank=2),
        _target("can_view_order", path="app/auth.py", rank=3),
    )
    ordered = assign_review_priority(targets, policy_reasons={**policy, "target:can_view_order": auth})
    assert [(t.unit_key, t.priority.attention) for t in ordered] == [
        ("can_view_order", "review_first"),
        ("apply_discount", "review_first"),
        ("order_total", "understand"),
    ]
    assert ordered[1].priority.reasons[0] == QueueReason("removed_safeguard", fact)
    # A judgment is never reopened by a safeguard fact.
    reviewed = assign_review_priority((_target("apply_discount", state="reviewed"),), policy_reasons=policy)
    assert reviewed[0].priority.tier == TIER_DONE


def test_review_first_membership_survives_a_judgment_and_never_comes_from_the_reviewers_own_marks() -> None:
    """The Brief's Understanding counts these parts: a mark moves a part's tier, never its membership."""

    from lemoncrow.pro.capabilities.review.review_priority import priority_payload

    policy = {
        "target:view": [QueueReason("sensitive_path_attention", "Access-control code changed")],
        "target:wide": [QueueReason("wide_reach", "Called from 1 file outside this change")],
        "target:disc": [QueueReason("removed_safeguard", "No longer rejects a percent over 100")],
    }
    targets = (
        _target("view", state="reviewed"),
        _target("wide", state="needs_changes"),
        _target("disc"),
        _target("total", state="needs_changes"),
        _target("again", state="changed_since_review"),
        _target("failed", fail=1, state="reviewed"),
    )
    by_key = {t.unit_key: t.priority for t in assign_review_priority(targets, policy_reasons=policy)}
    assert by_key["view"].tier == TIER_DONE and by_key["view"].review_first
    assert by_key["wide"].tier == TIER_POLICY_BLOCKER and by_key["wide"].review_first
    assert by_key["disc"].tier == TIER_REVIEW_FIRST and by_key["disc"].review_first
    # The reviewer's own objection or a re-review is not a reason to look first at the change.
    assert by_key["total"].tier == TIER_POLICY_BLOCKER and not by_key["total"].review_first
    assert by_key["again"].tier == TIER_RE_REVIEW and not by_key["again"].review_first
    # A failing linked check is the part's own evidence.
    assert by_key["failed"].review_first
    assert priority_payload(by_key["view"])["review_first"] is True
